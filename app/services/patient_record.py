"""C3 — PatientRecord: sectioned patient chart reads + unified assessment mapper."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.mappers import format_size_bytes
from app.core.diagnosis_catalog import diagnosis_labels
from app.core.utils import calculate_age, guardian_label
from app.models.assessment import Assessment
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.models.timeline import TimelineEvent
from app.schemas.patient import CaregiverResponse, PatientDetail, PatientSummary
from app.services.patient import build_clinical_domains, get_patient_aggregates


def map_assessment(
    assessment: Assessment,
    protocol_name: str,
    professional_name: str,
    *,
    patient: Patient | None = None,
) -> dict[str, Any]:
    """Unified assessment dict — one shape for detail include and list endpoints."""
    return {
        "id": str(assessment.id),
        "protocol": protocol_name,
        "protocolId": assessment.protocol_id,
        "date": assessment.date.isoformat(),
        "professional": professional_name,
        "result": assessment.result,
        "percentage": assessment.percentage,
        "interpretation": assessment.interpretation,
        "fields": assessment.fields or [],
        "status": assessment.status,
        "answers": assessment.answers or {},
        "scores": assessment.scores,
        "informant": assessment.informant,
        "patientId": str(patient.id) if patient else None,
        "patientName": patient.name if patient else None,
        "avatarColor": patient.avatar_color if patient else None,
    }


def _caregiver_response(c: Caregiver) -> CaregiverResponse:
    return CaregiverResponse(
        id=str(c.id),
        name=c.name,
        relation=c.relation,
        phone=c.phone,
        email=c.email,
        notes=c.notes,
        is_primary=c.is_primary,
        whatsapp_opt_in=c.whatsapp_opt_in,
    )


def _summary_from_aggregates(
    patient: Patient,
    professional: Professional,
    aggregates: dict,
    caregivers: list[Caregiver],
) -> PatientSummary:
    gl = guardian_label(caregivers)
    last = aggregates["last_session"]
    keys = patient.diagnosis_keys or []
    labels = diagnosis_labels(keys, professional.specialty_key)
    return PatientSummary(
        id=str(patient.id),
        name=patient.name,
        age=calculate_age(patient.birth_date),
        birth_date=patient.birth_date.isoformat(),
        address=patient.address,
        guardian=gl,
        guardian_label=gl,
        diagnoses=labels,
        diagnosis_keys=keys,
        therapist=professional.name,
        status=patient.status,
        start_date=patient.start_date.isoformat(),
        last_session=last.isoformat() if last else None,
        sessions_count=aggregates["sessions_count"],
        protocols_done=aggregates["protocols_done"],
        goals_achieved=aggregates["goals_achieved"],
        total_goals=aggregates["total_goals"],
        avatar_color=patient.avatar_color,
        is_demo=patient.is_demo,
        therapy_plan_content=patient.therapy_plan_content,
        therapy_plan_updated_at=patient.therapy_plan_updated_at.isoformat()
        if patient.therapy_plan_updated_at
        else None,
    )


async def build_patient_detail(
    db: AsyncSession,
    patient: Patient,
    professional: Professional,
    include: set[str],
) -> PatientDetail:
    """Compose PatientDetail from optional sections. Empty include ⇒ all sections."""
    aggregates = await get_patient_aggregates(db, patient.id)
    caregivers = (
        await db.execute(select(Caregiver).where(Caregiver.patient_id == patient.id))
    ).scalars().all()
    summary = _summary_from_aggregates(patient, professional, aggregates, list(caregivers))
    detail_data = summary.model_dump(by_alias=False)

    detail_data["caregivers"] = [_caregiver_response(c) for c in caregivers]
    detail_data["goals"] = []
    detail_data["clinical_domains"] = []
    detail_data["assessments"] = []
    detail_data["sessions"] = []
    detail_data["timeline"] = []
    detail_data["files"] = []

    if "goals" in include or not include:
        goals = (await db.execute(select(Goal).where(Goal.patient_id == patient.id))).scalars().all()
        detail_data["goals"] = [
            {
                "id": str(g.id),
                "title": g.title,
                "progress": g.progress,
                "area": g.area,
                "professional": professional.name,
                "startDate": g.start_date.isoformat(),
                "status": g.status,
            }
            for g in goals
        ]

    if "clinicalDomains" in include or "clinical_domains" in include or not include:
        detail_data["clinical_domains"] = await build_clinical_domains(db, patient.id)

    if "assessments" in include or not include:
        assessments = (
            await db.execute(
                select(Assessment)
                .where(Assessment.patient_id == patient.id)
                .options(selectinload(Assessment.protocol))
            )
        ).scalars().all()
        detail_data["assessments"] = [
            map_assessment(
                a,
                a.protocol.name if a.protocol else a.protocol_id,
                professional.name,
            )
            for a in assessments
        ]

    if "sessions" in include or not include:
        sessions = (
            await db.execute(select(Session).where(Session.patient_id == patient.id).order_by(Session.date.desc()))
        ).scalars().all()
        detail_data["sessions"] = [
            {
                "id": str(s.id),
                "date": s.date.isoformat(),
                "duration": s.duration,
                "therapist": professional.name,
                "objectives": s.objectives or [],
                "notes": s.notes,
                "type": s.type,
            }
            for s in sessions
        ]

    if "timeline" in include or not include:
        events = (
            await db.execute(
                select(TimelineEvent)
                .where(TimelineEvent.patient_id == patient.id)
                .order_by(TimelineEvent.date.desc())
            )
        ).scalars().all()
        detail_data["timeline"] = [
            {
                "id": str(e.id),
                "type": e.type,
                "title": e.title,
                "description": e.description,
                "date": e.date.isoformat(),
                "patientId": str(patient.id),
                "sourceId": str(e.source_id) if e.source_id else None,
            }
            for e in events
        ]

    if "files" in include or not include:
        files = (
            await db.execute(
                select(Attachment).where(Attachment.patient_id == patient.id).order_by(Attachment.date.desc())
            )
        ).scalars().all()
        detail_data["files"] = [
            {
                "id": str(f.id),
                "name": f.name,
                "category": f.category,
                "date": f.date.isoformat(),
                "sizeBytes": f.size_bytes,
                "size": format_size_bytes(f.size_bytes),
            }
            for f in files
        ]

    return PatientDetail(**detail_data)
