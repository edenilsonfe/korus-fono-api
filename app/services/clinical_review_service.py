"""B1/D1 clinical review workflow.

The service owns source revalidation and transaction boundaries; HTTP routes
only translate path/query/body data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.utils import goal_status_from_progress, utcnow
from app.models.ai import AIReport
from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.finance import PackageUsage, ReceivableItem
from app.models.goal import Goal
from app.models.home_program import HomeProgram, HomeProgramCheckIn, HomeProgramGrant, HomeProgramTask
from app.models.intervention_program import InterventionProgram, ProgramMeasurement
from app.models.family_portal import FamilyPortal, FamilyPortalGrant, FamilyPortalRecipient
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.clinical_review import (
    AppointmentDecision,
    ClinicalReviewComplete,
    ClinicalReviewCreate,
    ClinicalReviewUpdate,
    DischargeAppointmentPreview,
    DischargeFamilyGrantPreview,
    DischargeProgramPreview,
    DischargePreview,
    DueClinicalReviewResponse,
    FamilyReportCreate,
    FamilyPortalGrantPreview,
    GoalChange,
    GoalDecision,
    ReviewSourceInput,
    ReviewSourceSnapshot,
)
from app.services.assessment_comparison import compare_assessments
from app.services.patient_access import PatientAccess, has_permission, resolve_clinical_patient_access
from app.services.patient_appointment_service import cancel_selected_future_patient_appointments
from app.services.timeline import create_timeline_event
from app.models.clinical_review import ClinicalReview
from app.models.google_calendar import GoogleCalendarSyncRecord


def _error(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


async def _access(db: AsyncSession, patient_id: UUID, actor: Professional, permission: str = "clinical:read") -> PatientAccess:
    access = await resolve_clinical_patient_access(db, patient_id, actor)
    if access is None or not has_permission(access, permission):
        raise _error(status.HTTP_404_NOT_FOUND, "Paciente não encontrado")
    return access


async def _workflow_flag(db: AsyncSession, patient: Patient, key: str) -> None:
    # Root owns the shared rollout helper. Keeping this import local allows
    # migrations/tests that inspect models without loading feature services.
    from app.services.clinical_workflow_flags import require_workflow_enabled

    await require_workflow_enabled(db, patient.professional_id, key)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_key(source: ReviewSourceInput | dict) -> tuple[str, UUID, UUID | None]:
    if isinstance(source, ReviewSourceInput):
        return source.kind, source.source_id, source.comparison_target_id
    target = source.get("comparisonTargetId") or source.get("comparison_target_id")
    return str(source["kind"]), UUID(str(source.get("sourceId") or source.get("source_id"))), UUID(str(target)) if target else None


def _source_input_dict(source: ReviewSourceInput) -> dict:
    return {
        "kind": source.kind,
        "sourceId": str(source.source_id),
        "comparisonTargetId": str(source.comparison_target_id) if source.comparison_target_id else None,
        "version": source.version or 1,
        "fingerprint": source.fingerprint,
    }


def _source_fingerprint(sources: list[dict]) -> str:
    return _hash(
        sorted(
            [{k: item.get(k) for k in ("kind", "sourceId", "comparisonTargetId", "version", "fingerprint")} for item in sources],
            key=lambda item: (item["kind"], item["sourceId"], item["comparisonTargetId"] or ""),
        )
    )


def _source_input_from_snapshot(snapshot: dict) -> ReviewSourceInput:
    """Rehydrate only the source identity from its stored snapshot."""
    return ReviewSourceInput.model_validate(
        {
            key: snapshot.get(key)
            for key in ("kind", "sourceId", "comparisonTargetId", "version", "fingerprint")
        }
    )


def _validate_source_period(sources: list[dict], period_start: date | None, period_end: date | None) -> None:
    if not period_start and not period_end:
        return
    for source in sources:
        raw_date = source.get("sourceDate")
        if not raw_date:
            continue
        source_date = date.fromisoformat(raw_date)
        if period_start and source_date < period_start or period_end and source_date > period_end:
            raise _error(422, "Uma fonte selecionada está fora do período da revisão")


def _source_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


_FAMILY_OBSERVATION_LABELS = {
    "independent": "Sozinho",
    "with_support": "Com ajuda",
    "not_observed": "Ainda não observei",
    "no_opportunity": "Não houve oportunidade",
}
_FAMILY_CONTEXT_LABELS = {"home": "Casa", "school": "Escola", "other": "Outro"}


def _snapshot(
    *, kind: str, source_id: UUID, comparison_target_id: UUID | None = None,
    version: int, fingerprint: str, source_date: date | None,
    excerpt: str | None = None, author_id: UUID | None = None, author_name: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "sourceId": str(source_id),
        "comparisonTargetId": str(comparison_target_id) if comparison_target_id else None,
        "version": version,
        "fingerprint": fingerprint,
        "sourceDate": source_date.isoformat() if source_date else None,
        "excerpt": (excerpt or "")[:1000] or None,
        "authorId": str(author_id) if author_id else None,
        "authorName": author_name,
        "available": True,
    }


async def _load_source(db: AsyncSession, patient_id: UUID, kind: str, source_id: UUID, comparison_target_id: UUID | None = None) -> dict:
    """Load one source through its real patient/status boundary."""
    row: Any = None
    author_id: UUID | None = None
    author_name: str | None = None
    source_date: date | None = None
    excerpt = ""
    if kind == "comparison":
        if comparison_target_id is None:
            raise _error(422, "Uma comparação precisa de comparisonTargetId")
        comparison = await compare_assessments(db, patient_id=patient_id, base_id=source_id, target_id=comparison_target_id)
        target_assessment = await db.scalar(
            select(Assessment).where(Assessment.id == UUID(comparison.target.id), Assessment.patient_id == patient_id)
        )
        target_author_name = (
            await db.scalar(select(Professional.name).where(Professional.id == target_assessment.professional_id))
            if target_assessment
            else None
        )
        fingerprint = _hash({
            "base": comparison.base.__dict__,
            "target": comparison.target.__dict__,
            "percentageDelta": comparison.percentage_delta,
            "metrics": [metric.__dict__ for metric in comparison.metrics],
            "answersChanged": comparison.answers_changed,
        })
        return _snapshot(
            kind=kind,
            source_id=source_id,
            comparison_target_id=comparison_target_id,
            version=1,
            fingerprint=fingerprint,
            source_date=date.fromisoformat(comparison.target.date),
            excerpt=comparison.summary,
            author_id=target_assessment.professional_id if target_assessment else None,
            author_name=target_author_name,
        )
    if kind == "assessment":
        row = await db.scalar(select(Assessment).where(Assessment.id == source_id, Assessment.patient_id == patient_id, Assessment.status == "completed"))
        if row:
            source_date = row.date
            excerpt = f"{row.result} — {row.interpretation}".strip(" —")
            author_id = row.professional_id
    elif kind == "evolution":
        row = await db.scalar(select(Evolution).where(Evolution.id == source_id, Evolution.patient_id == patient_id))
        if row:
            source_date = _source_date(row.date)
            excerpt = f"{row.title or ''} {row.content}".strip()
            author_id = row.professional_id
    elif kind == "session":
        row = await db.scalar(select(Session).where(Session.id == source_id, Session.patient_id == patient_id))
        if row:
            source_date = _source_date(row.date)
            excerpt = f"{row.type} {row.notes}".strip()
            author_id = row.professional_id
    elif kind == "goal":
        row = await db.scalar(select(Goal).where(Goal.id == source_id, Goal.patient_id == patient_id))
        if row:
            source_date = row.start_date
            excerpt = f"{row.title} — {row.area}; progresso {row.progress}%"
            author_id = row.professional_id
    elif kind == "program_measurement":
        row = await db.scalar(
            select(ProgramMeasurement)
            .join(InterventionProgram, InterventionProgram.id == ProgramMeasurement.program_id)
            .where(
                ProgramMeasurement.id == source_id,
                InterventionProgram.patient_id == patient_id,
                ProgramMeasurement.review_status == "approved",
            )
        )
        if row:
            source_date = _source_date(row.recorded_at)
            excerpt = row.notes or f"{row.independent}/{row.opportunities} respostas independentes"
            author_id = row.recorded_by_professional_id
    elif kind == "family_checkin":
        row = await db.scalar(
            select(HomeProgramCheckIn)
            .join(HomeProgramTask, HomeProgramTask.id == HomeProgramCheckIn.task_id)
            .join(HomeProgram, HomeProgram.id == HomeProgramCheckIn.program_id)
            .where(HomeProgramCheckIn.id == source_id, HomeProgram.patient_id == patient_id)
        )
        if row:
            source_date = _source_date(row.responded_at)
            question = getattr(row, "functional_question", None)
            functional = getattr(row, "functional_observation", None)
            context = getattr(row, "observation_context", None)
            report = " — ".join(
                value for value in (
                    _FAMILY_OBSERVATION_LABELS.get(functional, functional) if functional else None,
                    row.comment,
                ) if value
            )
            excerpt = " — ".join(
                value for value in (
                    f"Pergunta funcional: {question}" if question else None,
                    f"Relato autodeclarado da família: {report}" if report else None,
                    f"Contexto: {_FAMILY_CONTEXT_LABELS.get(context, context)}" if context else None,
                    "Concluída" if row.done and not any((question, report, context)) else None,
                    "Não concluída" if not row.done and not any((question, report, context)) else None,
                ) if value
            )
    else:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Tipo de fonte não suportado: {kind}")
    if row is None:
        raise _error(status.HTTP_409_CONFLICT, "Uma fonte selecionada não está mais disponível")

    payload = {
        "kind": kind,
        "id": str(source_id),
        "date": source_date.isoformat() if source_date else None,
        "value": excerpt,
        "updatedAt": getattr(row, "updated_at", None),
        "version": getattr(row, "version", 1),
        "content": {
            key: getattr(row, key)
            for key in ("status", "result", "percentage", "interpretation", "title", "content", "progress", "done", "comment", "functional_observation", "observation_context")
            if hasattr(row, key)
        },
    }
    fingerprint = _hash(payload)
    version_value = getattr(row, "version", None)
    version = int(version_value) if isinstance(version_value, int) and version_value > 0 else 1
    if author_id:
        author_name = await db.scalar(select(Professional.name).where(Professional.id == author_id))
    return _snapshot(
        kind=kind,
        source_id=source_id,
        comparison_target_id=comparison_target_id,
        version=version,
        fingerprint=fingerprint,
        source_date=source_date,
        excerpt=excerpt,
        author_id=author_id,
        author_name=author_name,
    )


async def capture_sources(db: AsyncSession, patient_id: UUID, sources: list[ReviewSourceInput]) -> tuple[list[dict], str]:
    if len({(_source_key(source)) for source in sources}) != len(sources):
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Não repita a mesma fonte")
    snapshots = [await _load_source(db, patient_id, source.kind, source.source_id, source.comparison_target_id) for source in sources]
    return snapshots, _source_fingerprint(snapshots)


async def _validate_home_programs(db: AsyncSession, patient_id: UUID, program_ids: list[UUID]) -> None:
    if not program_ids:
        return
    found = set((await db.scalars(select(HomeProgram.id).where(HomeProgram.patient_id == patient_id, HomeProgram.id.in_(program_ids)))).all())
    if found != set(program_ids):
        raise _error(status.HTTP_409_CONFLICT, "Um programa de continuidade não pertence ao paciente")


def _review_response_data(review: ClinicalReview) -> dict:
    return {
        "id": review.id,
        "patient_id": review.patient_id,
        "author_professional_id": review.author_professional_id,
        "kind": review.kind,
        "status": review.status,
        "version": review.version,
        "period_start": review.period_start,
        "period_end": review.period_end,
        "summary": review.summary,
        "goal_decisions": review.goal_decisions or [],
        "sources": review.source_snapshot or [],
        "source_fingerprint": review.source_fingerprint,
        "next_review_on": review.next_review_on,
        "completed_at": review.completed_at,
        "completed_by_professional_id": review.completed_by_professional_id,
        "supersedes_review_id": review.supersedes_review_id,
        "discharge_on": review.discharge_on,
        "discharge_reason": review.discharge_reason,
        "final_summary": review.final_summary,
        "family_guidance": review.family_guidance,
        "return_recommended": review.return_recommended,
        "return_on": review.return_on,
        "home_program_ids": [UUID(str(value)) for value in (review.home_program_ids or [])],
        "agenda_fingerprint": review.agenda_fingerprint,
        "appointment_decisions": review.appointment_decisions or [],
        "family_report_id": review.family_report_id,
    }


async def create_review(db: AsyncSession, patient_id: UUID, actor: Professional, body: ClinicalReviewCreate) -> ClinicalReview:
    access = await _access(db, patient_id, actor, "clinical:write")
    await _workflow_flag(db, access.patient, "clinical_reviews")
    if body.kind == "discharge":
        if not access.is_owner:
            raise _error(403, "Somente o profissional dono pode preparar a alta")
        await _workflow_flag(db, access.patient, "structured_discharge")
    if body.supersedes_review_id:
        predecessor = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == body.supersedes_review_id, ClinicalReview.patient_id == patient_id, ClinicalReview.status == "completed"))
        if predecessor is None:
            raise _error(409, "A revisão de origem da correção não foi encontrada")
    sources, fingerprint = await capture_sources(db, patient_id, body.sources)
    _validate_source_period(sources, body.period_start, body.period_end)
    await _validate_home_programs(db, patient_id, body.home_program_ids)
    review = ClinicalReview(
        patient_id=patient_id,
        author_professional_id=actor.id,
        kind=body.kind,
        status="draft",
        period_start=body.period_start,
        period_end=body.period_end,
        summary=body.summary,
        goal_decisions=[item.model_dump(mode="json", by_alias=True) for item in body.goal_decisions],
        source_snapshot=sources,
        source_fingerprint=fingerprint,
        next_review_on=body.next_review_on,
        supersedes_review_id=body.supersedes_review_id,
        discharge_on=body.discharge_on,
        discharge_reason=body.discharge_reason,
        final_summary=body.final_summary,
        family_guidance=body.family_guidance,
        return_recommended=body.return_recommended,
        return_on=body.return_on,
        home_program_ids=[str(value) for value in body.home_program_ids],
    )
    db.add(review)
    await db.flush()
    return review


async def get_review(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional, *, write: bool = False) -> ClinicalReview:
    await _access(db, patient_id, actor, "clinical:write" if write else "clinical:read")
    review = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == review_id, ClinicalReview.patient_id == patient_id))
    if review is None:
        raise _error(404, "Revisão clínica não encontrada")
    return review


async def list_reviews(db: AsyncSession, patient_id: UUID, actor: Professional, *, page: int, limit: int, status_filter: str | None = None):
    await _access(db, patient_id, actor)
    query = select(ClinicalReview).where(ClinicalReview.patient_id == patient_id)
    count_query = select(func.count()).select_from(ClinicalReview).where(ClinicalReview.patient_id == patient_id)
    if status_filter:
        query = query.where(ClinicalReview.status == status_filter)
        count_query = count_query.where(ClinicalReview.status == status_filter)
    total = int(await db.scalar(count_query) or 0)
    rows = (await db.scalars(query.order_by(ClinicalReview.created_at.desc(), ClinicalReview.id.desc()).offset((page - 1) * limit).limit(limit))).all()
    return rows, total


async def update_review(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional, body: ClinicalReviewUpdate) -> ClinicalReview:
    review = await get_review(db, patient_id, review_id, actor, write=True)
    review_patient = await db.get(Patient, patient_id)
    access = await _access(db, patient_id, actor, "clinical:write")
    if review.kind == "discharge" and not access.is_owner:
        raise _error(403, "Somente o profissional dono pode editar a alta")
    await _workflow_flag(db, review_patient, "clinical_reviews")
    locked = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == review.id).with_for_update().execution_options(populate_existing=True))
    if locked is None:
        raise _error(404, "Revisão clínica não encontrada")
    if locked.status != "draft":
        raise _error(409, "Revisões concluídas ou canceladas são imutáveis")
    if locked.version != body.expected_version:
        raise _error(409, "O rascunho foi alterado por outra sessão")
    if locked.kind == "discharge":
        await _workflow_flag(db, review_patient, "structured_discharge")
    values = body.model_dump(exclude_unset=True, by_alias=False)
    values.pop("expected_version", None)
    period_start = values.get("period_start", locked.period_start)
    period_end = values.get("period_end", locked.period_end)
    if period_start and period_end and period_end < period_start:
        raise _error(422, "O fim do período não pode ser anterior ao início")
    if "sources" in values:
        source_inputs = [ReviewSourceInput.model_validate(item) for item in values.pop("sources") or []]
        snapshots, fingerprint = await capture_sources(db, patient_id, source_inputs)
        _validate_source_period(snapshots, period_start, period_end)
        locked.source_snapshot = snapshots
        locked.source_fingerprint = fingerprint
    else:
        _validate_source_period(
            locked.source_snapshot or [],
            period_start,
            period_end,
        )
    if "home_program_ids" in values and values["home_program_ids"] is not None:
        await _validate_home_programs(db, patient_id, values["home_program_ids"])
    for field, value in values.items():
        if field == "goal_decisions" and value is not None:
            value = [GoalDecision.model_validate(item).model_dump(mode="json", by_alias=True) for item in value]
        if field == "home_program_ids" and value is not None:
            value = [str(item) for item in value]
        setattr(locked, field, value)
    locked.version += 1
    await db.flush()
    return locked


async def cancel_review(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional, expected_version: int) -> ClinicalReview:
    access = await _access(db, patient_id, actor, "clinical:write")
    review = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == review_id, ClinicalReview.patient_id == patient_id).with_for_update().execution_options(populate_existing=True))
    if review is None:
        raise _error(404, "Revisão clínica não encontrada")
    if review.kind == "discharge" and not access.is_owner:
        raise _error(403, "Somente o profissional dono pode cancelar a alta")
    if review.status != "draft":
        return review
    if review.version != expected_version:
        raise _error(409, "O rascunho foi alterado por outra sessão")
    review.status = "cancelled"
    review.version += 1
    await db.flush()
    return review


async def list_sources(db: AsyncSession, patient_id: UUID, actor: Professional, *, from_date: date | None, to_date: date | None, kind: str | None, page: int, limit: int):
    await _access(db, patient_id, actor)
    kinds = [kind] if kind else ["assessment", "comparison", "evolution", "session", "goal", "program_measurement", "family_checkin"]
    collected: list[dict] = []
    for source_kind in kinds:
        if source_kind == "comparison":
            assessments = list((await db.scalars(
                select(Assessment)
                .where(Assessment.patient_id == patient_id, Assessment.status == "completed")
                .order_by(Assessment.protocol_id, Assessment.date, Assessment.created_at, Assessment.id)
            )).all())
            grouped: dict[str, list[Assessment]] = {}
            for assessment in assessments:
                grouped.setdefault(assessment.protocol_id, []).append(assessment)
            for pair in grouped.values():
                for base, target in zip(pair, pair[1:]):
                    candidate = await _load_source(db, patient_id, "comparison", base.id, target.id)
                    source_date = date.fromisoformat(candidate["sourceDate"]) if candidate.get("sourceDate") else None
                    if from_date and (source_date is None or source_date < from_date):
                        continue
                    if to_date and (source_date is None or source_date > to_date):
                        continue
                    collected.append({**candidate, "label": f"Comparação {base.protocol_id}: {str(base.id)[:8]} → {str(target.id)[:8]}"})
            continue
        if source_kind == "assessment":
            ids = list((await db.scalars(select(Assessment.id).where(Assessment.patient_id == patient_id, Assessment.status == "completed"))).all())
        elif source_kind == "evolution":
            ids = list((await db.scalars(select(Evolution.id).where(Evolution.patient_id == patient_id))).all())
        elif source_kind == "session":
            ids = list((await db.scalars(select(Session.id).where(Session.patient_id == patient_id))).all())
        elif source_kind == "goal":
            ids = list((await db.scalars(select(Goal.id).where(Goal.patient_id == patient_id))).all())
        elif source_kind == "program_measurement":
            ids = list((await db.scalars(select(ProgramMeasurement.id).join(InterventionProgram, InterventionProgram.id == ProgramMeasurement.program_id).where(InterventionProgram.patient_id == patient_id, ProgramMeasurement.review_status == "approved"))).all())
        elif source_kind == "family_checkin":
            ids = list((await db.scalars(select(HomeProgramCheckIn.id).join(HomeProgram, HomeProgram.id == HomeProgramCheckIn.program_id).where(HomeProgram.patient_id == patient_id))).all())
        else:
            raise _error(422, f"Tipo de fonte não suportado: {source_kind}")
        for source_id in ids:
            try:
                candidate = await _load_source(db, patient_id, source_kind, source_id)
            except HTTPException:
                continue
            source_date = date.fromisoformat(candidate["sourceDate"]) if candidate.get("sourceDate") else None
            if from_date and (source_date is None or source_date < from_date):
                continue
            if to_date and (source_date is None or source_date > to_date):
                continue
            label = f"{source_kind}:{str(source_id)[:8]}"
            collected.append({**candidate, "label": label})
    collected.sort(key=lambda item: (item.get("sourceDate") or "", item["sourceId"]), reverse=True)
    total = len(collected)
    page_rows = collected[(page - 1) * limit : page * limit]
    return page_rows, total


async def _future_appointments(db: AsyncSession, patient_id: UUID, professional_id: UUID, *, lock: bool = False) -> list[tuple[Appointment, bool, bool]]:
    clinic_now = datetime.now(ZoneInfo(get_settings().clinic_timezone))
    query = select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.professional_id == professional_id,
        Appointment.date >= clinic_now.date(),
        Appointment.status.in_(("pendente", "confirmado")),
    ).order_by(Appointment.date, Appointment.time, Appointment.id)
    if lock:
        query = query.with_for_update()
    appointments = [item for item in (await db.scalars(query)).all() if datetime.combine(item.date, item.time, tzinfo=clinic_now.tzinfo) > clinic_now]
    if not appointments:
        return []
    ids = [item.id for item in appointments]
    session_ids = set((await db.scalars(select(Session.appointment_id).where(Session.appointment_id.in_(ids)))).all())
    finance_ids = set((await db.scalars(select(ReceivableItem.appointment_id).where(ReceivableItem.appointment_id.in_(ids)))).all())
    finance_ids.update((await db.scalars(select(PackageUsage.appointment_id).where(PackageUsage.appointment_id.in_(ids)))).all())
    return [(item, item.id in session_ids, item.id in finance_ids) for item in appointments]


def _agenda_fingerprint(rows: list[tuple[Appointment, bool, bool]]) -> str:
    return _hash([
        {"id": str(item.id), "date": item.date.isoformat(), "time": item.time.isoformat(), "duration": item.duration, "status": item.status, "session": session, "finance": finance}
        for item, session, finance in rows
    ])


async def discharge_preview(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional) -> DischargePreview:
    access = await _access(db, patient_id, actor, "clinical:write")
    if not access.is_owner:
        raise _error(403, "Somente o profissional dono pode conferir a alta")
    review = await get_review(db, patient_id, review_id, actor, write=True)
    if review.kind != "discharge":
        raise _error(409, "A prévia de agenda só existe para uma alta")
    patient = await db.get(Patient, patient_id)
    rows = await _future_appointments(db, patient_id, patient.professional_id)
    items = []
    for appointment, has_session, has_finance in rows:
        blocking = "A consulta possui sessão vinculada" if has_session else ("A consulta possui vínculo financeiro" if has_finance else None)
        items.append(DischargeAppointmentPreview(
            id=appointment.id, date=appointment.date, time=appointment.time.isoformat(), duration=appointment.duration,
            type=appointment.type, status=appointment.status, can_cancel=not blocking, blocking_reason=blocking,
            has_session=has_session, has_financial_link=has_finance,
        ))
    program_ids = [UUID(str(value)) for value in (review.home_program_ids or [])]
    # Keep the selected IDs safe even for drafts created before this
    # validation existed.  The preview itself is broader: the clinician must
    # be able to choose any current continuation program for the discharge.
    await _validate_home_programs(db, patient_id, program_ids)
    programs = list((await db.scalars(
        select(HomeProgram).where(
            HomeProgram.patient_id == patient_id,
            HomeProgram.status == "active",
        ).order_by(HomeProgram.starts_on, HomeProgram.id)
    )).all())
    grants = list((await db.scalars(
        select(HomeProgramGrant)
        .join(HomeProgram, HomeProgram.id == HomeProgramGrant.program_id)
        .where(
            HomeProgram.patient_id == patient_id,
            HomeProgramGrant.revoked_at.is_(None),
            HomeProgramGrant.expires_at > utcnow(),
        )
    )).all())
    portal_grants = list((await db.scalars(
        select(FamilyPortalGrant)
        .join(FamilyPortalRecipient, FamilyPortalRecipient.id == FamilyPortalGrant.recipient_id)
        .join(FamilyPortal, FamilyPortal.id == FamilyPortalRecipient.portal_id)
        .where(
            FamilyPortal.patient_id == patient_id,
            FamilyPortal.enabled.is_(True),
            FamilyPortalRecipient.active.is_(True),
            FamilyPortalGrant.revoked_at.is_(None),
            FamilyPortalGrant.expires_at > utcnow(),
        )
    )).all())
    return DischargePreview(
        review_id=review.id,
        agenda_fingerprint=_agenda_fingerprint(rows),
        appointments=items,
        programs=[
            DischargeProgramPreview(
                id=item.id, title=item.title, status=item.status,
                starts_on=item.starts_on, ends_on=item.ends_on,
                active_grant=any(grant.program_id == item.id for grant in grants),
            )
            for item in programs
        ],
        family_grants=[
            DischargeFamilyGrantPreview(
                id=grant.id, program_id=grant.program_id,
                caregiver_name=grant.caregiver_name_snapshot,
                expires_at=grant.expires_at,
            )
            for grant in grants
        ],
        family_portal_grants=[
            FamilyPortalGrantPreview(
                id=grant.id, recipient_id=grant.recipient_id,
                expires_at=grant.expires_at,
            )
            for grant in portal_grants
        ],
    )


async def _validate_goal_changes(db: AsyncSession, patient_id: UUID, changes: list[GoalChange]) -> list[Goal]:
    if not changes:
        return []
    ids = [item.goal_id for item in changes]
    goals = list((await db.scalars(select(Goal).where(Goal.patient_id == patient_id, Goal.id.in_(ids)).with_for_update())).all())
    if len(goals) != len(set(ids)):
        raise _error(409, "Uma meta selecionada não pertence ao paciente")
    return goals


async def complete_review(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional, body: ClinicalReviewComplete) -> ClinicalReview:
    access = await _access(db, patient_id, actor, "clinical:write")
    await _workflow_flag(db, access.patient, "clinical_reviews")
    if not has_permission(access, "therapy_plan:write"):
        raise _error(403, "Seu papel não permite concluir revisões terapêuticas")
    review = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == review_id, ClinicalReview.patient_id == patient_id).with_for_update().execution_options(populate_existing=True))
    if review is None:
        raise _error(404, "Revisão clínica não encontrada")
    payload_hash = _hash(body.model_dump(mode="json", by_alias=True))
    if review.status == "completed":
        if review.completion_idempotency_key == body.idempotency_key and review.completion_payload_hash == payload_hash:
            return review
        raise _error(409, "Esta revisão já foi concluída; crie uma correção")
    if review.status != "draft":
        raise _error(409, "A revisão não está disponível para conclusão")
    if review.version != body.expected_version:
        raise _error(409, "O rascunho foi alterado por outra sessão")
    if not body.reviewed:
        raise _error(422, "Confirme que você revisou as fontes antes de concluir")
    if review.kind == "discharge":
        await _workflow_flag(db, access.patient, "structured_discharge")
        if not access.is_owner:
            raise _error(403, "Somente o profissional dono pode concluir a alta")
        if review.discharge_on is None or review.discharge_on > date.today():
            raise _error(422, "Informe uma data de alta que não seja futura")

    selected = [_source_input_from_snapshot(item) for item in review.source_snapshot or []]
    try:
        fresh_sources, fresh_fingerprint = await capture_sources(db, patient_id, selected)
    except HTTPException as exc:
        if exc.status_code in {status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND}:
            raise _error(status.HTTP_409_CONFLICT, "Uma fonte selecionada mudou; reconfirme as fontes antes de concluir") from exc
        raise
    _validate_source_period(fresh_sources, review.period_start, review.period_end)
    await _validate_home_programs(db, patient_id, [UUID(str(value)) for value in (review.home_program_ids or [])])
    if body.source_fingerprint != fresh_fingerprint:
        raise _error(409, "Uma fonte mudou; reconfirme as fontes antes de concluir")
    await _validate_goal_changes(db, patient_id, body.goal_changes)
    if body.goal_changes and not body.confirm_goal_changes:
        raise _error(422, "Confirme explicitamente as alterações de metas")
    if body.goal_changes and not has_permission(access, "therapy_plan:write"):
        raise _error(403, "Seu papel não permite alterar metas nesta conclusão")

    google_records: list[GoogleCalendarSyncRecord] = []
    if review.kind == "discharge":
        rows = await _future_appointments(db, patient_id, actor.id, lock=True)
        current_fingerprint = _agenda_fingerprint(rows)
        if body.expected_agenda_fingerprint != current_fingerprint:
            raise _error(409, "A agenda mudou; atualize a prévia antes de concluir")
        expected_ids = {item.id for item, _session, _finance in rows}
        decisions = {decision.appointment_id: decision.action for decision in body.appointment_decisions}
        if set(decisions) != expected_ids:
            raise _error(409, "Escolha manter ou cancelar para cada consulta futura")
        blocked = {item.id for item, session, finance in rows if session or finance}
        if any(action == "cancel" and appointment_id in blocked for appointment_id, action in decisions.items()):
            raise _error(409, "Uma consulta com vínculo de sessão ou financeiro não pode ser cancelada pela alta")
        cancel_ids = [appointment_id for appointment_id, action in decisions.items() if action == "cancel"]
        _cancelled, _events, google_records = await cancel_selected_future_patient_appointments(
            db, professional_id=actor.id, patient_id=patient_id, appointment_ids=cancel_ids, notify_via_whatsapp=False
        )
        review.agenda_fingerprint = current_fingerprint
        review.appointment_decisions = [item.model_dump(mode="json", by_alias=True) for item in body.appointment_decisions]
        access.patient.status = "alta"

    for change in body.goal_changes:
        goal = await db.scalar(select(Goal).where(Goal.id == change.goal_id, Goal.patient_id == patient_id).with_for_update())
        if goal is None:
            raise _error(409, "Uma meta selecionada não pertence ao paciente")
        for field in ("title", "area", "progress", "status"):
            value = getattr(change, field)
            if value is not None:
                setattr(goal, field, value)
        if change.progress is not None and change.status is None:
            goal.status = goal_status_from_progress(change.progress)

    review.source_snapshot = fresh_sources
    review.source_fingerprint = fresh_fingerprint
    review.status = "completed"
    review.version += 1
    review.completed_at = utcnow()
    review.completed_by_professional_id = actor.id
    review.completion_idempotency_key = body.idempotency_key
    review.completion_payload_hash = payload_hash
    await create_timeline_event(
        db, patient_id=patient_id, professional_id=actor.id,
        event_type="clinical_review" if review.kind == "periodic" else "alta",
        title="Revisão terapêutica concluída" if review.kind == "periodic" else "Alta clínica registrada",
        description=review.summary or review.final_summary or "",
        source_id=review.id,
    )
    await db.flush()
    review._queued_google_record_ids = [record.id for record in google_records]  # type: ignore[attr-defined]
    return review


async def create_family_report(db: AsyncSession, patient_id: UUID, review_id: UUID, actor: Professional, body: FamilyReportCreate) -> AIReport:
    access = await _access(db, patient_id, actor, "clinical:write")
    review = await db.scalar(select(ClinicalReview).where(ClinicalReview.id == review_id, ClinicalReview.patient_id == patient_id).with_for_update().execution_options(populate_existing=True))
    if review is None:
        raise _error(404, "Revisão clínica não encontrada")
    review_patient = await db.get(Patient, patient_id)
    if not access.is_owner:
        raise _error(403, "Somente o profissional dono pode gerar o relatório familiar")
    await _workflow_flag(db, review_patient, "clinical_reviews")
    if review.kind == "discharge":
        await _workflow_flag(db, review_patient, "structured_discharge")
    if review.status != "completed":
        raise _error(409, "Conclua a revisão antes de criar o relatório familiar")
    if review.family_report_id:
        report = await db.get(AIReport, review.family_report_id)
        if report:
            return report
    content = review.family_guidance or review.final_summary or review.summary
    if not content.strip():
        raise _error(422, "Escreva uma orientação antes de criar o relatório familiar")
    report = AIReport(
        professional_id=review_patient.professional_id, patient_id=patient_id, type="pais", date=review.completed_at.date() if review.completed_at else date.today(),
        preview=content[:200], content=content, status="draft",
    )
    db.add(report)
    await db.flush()
    review.family_report_id = report.id
    await create_timeline_event(db, patient_id=patient_id, professional_id=actor.id, event_type="relatorio", title="Rascunho de relatório familiar criado", description=report.preview, source_id=report.id)
    return report


async def list_due(db: AsyncSession, actor: Professional, *, limit: int = 50) -> list[DueClinicalReviewResponse]:
    discharge = aliased(ClinicalReview)
    newer_periodic = aliased(ClinicalReview)
    closed_by_discharge = exists(
        select(1).where(
            discharge.patient_id == ClinicalReview.patient_id,
            discharge.kind == "discharge",
            discharge.status == "completed",
            discharge.completed_at.is_not(None),
            or_(
                discharge.completed_at > ClinicalReview.completed_at,
                (discharge.completed_at == ClinicalReview.completed_at) & (discharge.id > ClinicalReview.id),
            ),
        )
    )
    superseded_by_periodic = exists(
        select(1).where(
            newer_periodic.patient_id == ClinicalReview.patient_id,
            newer_periodic.kind == "periodic",
            newer_periodic.status == "completed",
            newer_periodic.completed_at.is_not(None),
            or_(
                newer_periodic.completed_at > ClinicalReview.completed_at,
                (newer_periodic.completed_at == ClinicalReview.completed_at)
                & (newer_periodic.id > ClinicalReview.id),
            ),
        )
    )
    rows = (await db.execute(
        select(ClinicalReview, Patient.name)
        .join(Patient, Patient.id == ClinicalReview.patient_id)
        .where(
            ClinicalReview.status == "completed",
            ClinicalReview.kind == "periodic",
            ClinicalReview.next_review_on <= date.today(),
            Patient.professional_id == actor.id,
            ~closed_by_discharge,
            ~superseded_by_periodic,
        )
        .order_by(ClinicalReview.next_review_on, ClinicalReview.id)
        .limit(limit)
    )).all()
    return [DueClinicalReviewResponse(id=r.id, patient_id=r.patient_id, patient_name=name, next_review_on=r.next_review_on, kind=r.kind) for r, name in rows if r.next_review_on]


__all__ = [
    "create_review", "get_review", "list_reviews", "update_review", "cancel_review", "list_sources",
    "complete_review", "discharge_preview", "create_family_report", "list_due", "_review_response_data",
]
