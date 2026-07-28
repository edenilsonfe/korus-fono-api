from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.db.session import get_db
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.timeline import TimelineEvent
from app.schemas.patient import TimelineEventResponse

router = APIRouter(tags=["timeline"])


def _event_response(event: TimelineEvent, patient_name: str | None = None) -> TimelineEventResponse:
    return TimelineEventResponse(
        id=str(event.id),
        type=event.type,
        title=event.title,
        description=event.description,
        date=event.date.isoformat(),
        patient_id=str(event.patient_id),
        patient_name=patient_name,
        source_id=str(event.source_id) if event.source_id else None,
    )


@router.get("/timeline", response_model=list[TimelineEventResponse])
async def global_timeline(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(TimelineEvent, Patient.name)
        .join(Patient, TimelineEvent.patient_id == Patient.id)
        .where(TimelineEvent.professional_id == professional.id)
        .order_by(TimelineEvent.date.desc())
        .limit(limit)
    )
    if cursor:
        cursor_dt = datetime.fromisoformat(cursor)
        query = query.where(TimelineEvent.date < cursor_dt)
    result = await db.execute(query)
    return [_event_response(e, name) for e, name in result.all()]


patient_router = APIRouter(prefix="/patients/{patient_id}/timeline", tags=["timeline"])


@patient_router.get("", response_model=list[TimelineEventResponse])
async def patient_timeline(
    patient_id: UUID,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    query = (
        select(TimelineEvent)
        .where(TimelineEvent.patient_id == patient.id)
        .order_by(TimelineEvent.date.desc())
        .limit(limit)
    )
    if cursor:
        cursor_dt = datetime.fromisoformat(cursor)
        query = query.where(TimelineEvent.date < cursor_dt)
    result = await db.execute(query)
    return [_event_response(e, patient.name) for e in result.scalars().all()]
