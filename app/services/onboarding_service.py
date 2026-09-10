from datetime import UTC, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.models.ai import AIReport
from app.models.assessment import ASSESSMENT_STATUS_COMPLETED, Assessment
from app.models.finance import ServiceOffering
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.onboarding import OnboardingResponse, OnboardingSteps
from app.services.demo_patient_service import ensure_demo_patient

MCHAT_PROTOCOL_IDS = ("mchat", "m-chat-r", "m-chat")


async def _demo_patient(db: AsyncSession, professional_id) -> Patient | None:
    return await db.scalar(
        select(Patient)
        .where(Patient.professional_id == professional_id, Patient.is_demo.is_(True))
        .order_by(Patient.created_at.asc())
        .limit(1)
    )


async def _real_patient(db: AsyncSession, professional_id) -> Patient | None:
    return await db.scalar(
        select(Patient)
        .where(Patient.professional_id == professional_id, Patient.is_demo.is_(False))
        .order_by(Patient.created_at.asc())
        .limit(1)
    )


async def _configured_service(db: AsyncSession, professional_id) -> ServiceOffering | None:
    return await db.scalar(
        select(ServiceOffering)
        .where(ServiceOffering.professional_id == professional_id)
        .order_by(ServiceOffering.created_at.asc())
        .limit(1)
    )


async def _ensure_demo_while_onboarding(
    db: AsyncSession, professional: Professional
) -> Patient | None:
    demo = await _demo_patient(db, professional.id)
    if demo is not None or await _real_patient(db, professional.id) is not None:
        return demo
    return await ensure_demo_patient(db, professional)


async def _has_completed_demo_assessment(db: AsyncSession, demo_patient_id) -> bool:
    if demo_patient_id is None:
        return False
    assessment_id = await db.scalar(
        select(Assessment.id)
        .where(
            Assessment.patient_id == demo_patient_id,
            Assessment.status == ASSESSMENT_STATUS_COMPLETED,
            func.lower(Assessment.protocol_id).in_(MCHAT_PROTOCOL_IDS),
        )
        .limit(1)
    )
    return assessment_id is not None


async def build_onboarding_response(
    db: AsyncSession, professional: Professional
) -> OnboardingResponse:
    real_patient = await _real_patient(db, professional.id)
    configured_service = await _configured_service(db, professional.id)
    demo = await _ensure_demo_while_onboarding(db, professional)
    demo_id = demo.id if demo else None
    completed_assessment = await _has_completed_demo_assessment(db, demo_id)
    report_id = None
    if demo_id is not None:
        report_id = await db.scalar(
            select(AIReport.id)
            .where(
                AIReport.professional_id == professional.id,
                AIReport.patient_id == demo_id,
            )
            .order_by(AIReport.created_at.asc(), AIReport.id.asc())
            .limit(1)
        )

    steps = OnboardingSteps(
        viewed_demo_patient=professional.onboarding_viewed_demo_patient_at is not None,
        completed_demo_assessment=completed_assessment,
        viewed_demo_result=professional.onboarding_viewed_demo_result_at is not None,
        created_demo_report=report_id is not None,
        reviewed_demo_report=professional.onboarding_reviewed_demo_report_at is not None,
        configured_service=configured_service is not None,
        created_real_patient=real_patient is not None,
    )
    is_complete = steps.configured_service and steps.created_real_patient
    is_demo_complete = (
        steps.viewed_demo_patient and steps.viewed_demo_result and steps.reviewed_demo_report
    )
    completion_moments = [
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
        for moment in (
            professional.onboarding_completed_at,
            configured_service.created_at if configured_service is not None else None,
            real_patient.created_at if real_patient is not None else None,
        )
        if moment is not None
    ]
    completed_at = max(completion_moments) if is_complete else None

    if is_complete:
        next_step = "completed"
    elif steps.created_real_patient:
        next_step = "configure_service"
    elif is_demo_complete:
        next_step = "create_real_patient"
    elif not steps.viewed_demo_patient:
        next_step = "view_demo_patient"
    elif not steps.viewed_demo_result:
        next_step = "view_demo_result"
    elif not steps.created_demo_report:
        next_step = "create_demo_report"
    else:
        next_step = "review_demo_report"

    return OnboardingResponse(
        version=3,
        demo_patient_id=str(demo.id) if demo else None,
        demo_report_id=str(report_id) if report_id else None,
        demo_started_at=professional.onboarding_viewed_demo_patient_at,
        demo_completed_at=professional.onboarding_reviewed_demo_report_at if is_demo_complete else None,
        is_demo_complete=is_demo_complete,
        skipped_at=professional.onboarding_skipped_at,
        started_at=professional.onboarding_started_at or professional.created_at,
        completed_at=completed_at,
        dismissed_until=professional.onboarding_dismissed_until,
        is_complete=is_complete,
        next_step=next_step,
        steps=steps,
    )


async def update_onboarding(
    db: AsyncSession, professional: Professional, action: str, *, report_id: UUID | None = None
) -> OnboardingResponse:
    now = utcnow()
    if professional.onboarding_started_at is None:
        professional.onboarding_started_at = professional.created_at or now

    if action == "viewed_demo_patient":
        demo = await _ensure_demo_while_onboarding(db, professional)
        if demo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Paciente demonstração não encontrado",
            )
        if professional.onboarding_viewed_demo_patient_at is None:
            professional.onboarding_viewed_demo_patient_at = now
    elif action == "viewed_demo_result":
        demo = await _ensure_demo_while_onboarding(db, professional)
        if demo is None or professional.onboarding_viewed_demo_patient_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conheça o caso demonstrativo antes de ver sua evolução",
            )
        if professional.onboarding_viewed_demo_result_at is None:
            professional.onboarding_viewed_demo_result_at = now
    elif action == "reviewed_demo_report":
        demo = await _demo_patient(db, professional.id)
        report = None
        if demo is not None and report_id is not None:
            report = await db.scalar(
                select(AIReport).where(
                    AIReport.id == report_id,
                    AIReport.professional_id == professional.id,
                    AIReport.patient_id == demo.id,
                )
            )
        if report is None or not report.content.strip():
            raise HTTPException(status_code=404, detail="Relatório demonstrativo não encontrado")
        if (
            professional.onboarding_viewed_demo_patient_at is None
            or professional.onboarding_viewed_demo_result_at is None
        ):
            raise HTTPException(status_code=409, detail="Conheça o caso e sua evolução antes de concluir")
        # Reviewing the demonstration never finalizes or changes a clinical report.
        if professional.onboarding_reviewed_demo_report_at is None:
            professional.onboarding_reviewed_demo_report_at = now
    elif action == "skip":
        professional.onboarding_skipped_at = now
    elif action == "postpone":
        professional.onboarding_dismissed_until = now + timedelta(days=1)
    elif action == "resume":
        professional.onboarding_dismissed_until = None
        professional.onboarding_skipped_at = None
        await ensure_demo_patient(db, professional)

    await db.flush()
    return await build_onboarding_response(db, professional)
