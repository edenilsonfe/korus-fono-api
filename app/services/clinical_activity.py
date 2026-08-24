"""C4 — ClinicalActivity: typed timeline emission; callers don't build title/description."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.professional import Professional
from app.models.session import Session
from app.models.timeline import TimelineEvent
from app.services.timeline import create_timeline_event


@dataclass(frozen=True)
class EventRef:
    id: UUID
    event_type: str
    source_id: UUID | None


async def _emit(
    db: AsyncSession,
    *,
    patient_id: UUID,
    professional_id: UUID,
    event_type: str,
    title: str,
    description: str = "",
    source_id: UUID | None = None,
    date=None,
) -> TimelineEvent:
    event = await create_timeline_event(
        db,
        patient_id=patient_id,
        professional_id=professional_id,
        event_type=event_type,
        title=title,
        description=description,
        source_id=source_id,
        date=date,
    )
    await db.flush()
    return event


async def record_session(
    db: AsyncSession,
    *,
    session: Session,
    professional: Professional,
) -> TimelineEvent:
    notes = session.notes or ""
    return await _emit(
        db,
        patient_id=session.patient_id,
        professional_id=professional.id,
        event_type="sessao",
        title=f"Sessão de {session.type}",
        description=notes[:200],
        source_id=session.id,
        date=session.date,
    )


async def record_assessment(
    db: AsyncSession,
    *,
    assessment: Assessment,
    protocol_name: str,
    professional: Professional,
) -> TimelineEvent:
    return await _emit(
        db,
        patient_id=assessment.patient_id,
        professional_id=professional.id,
        event_type="avaliacao",
        title=f"Avaliação {protocol_name} aplicada",
        description=assessment.result or "",
        source_id=assessment.id,
    )


async def record_evolution(
    db: AsyncSession,
    *,
    evolution: Evolution,
    professional: Professional,
) -> TimelineEvent:
    return await _emit(
        db,
        patient_id=evolution.patient_id,
        professional_id=professional.id,
        event_type="evolucao",
        title=evolution.title or "Evolução registrada",
        description=(evolution.content or "")[:200],
        source_id=evolution.id,
    )
