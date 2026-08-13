from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, literal_column, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.core.utils import utcnow
from app.db.session import get_db
from app.models.appointment import Appointment
from app.models.evolution import Evolution
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.common import PaginatedResponse
from app.schemas.session import SessionCreate, SessionGlobalResponse, SessionUpdate
from app.services.clinical_activity import record_session

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=PaginatedResponse[SessionGlobalResponse])
async def list_sessions_global(
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Session, Patient)
        .join(Patient, Session.patient_id == Patient.id)
        .where(Patient.professional_id == professional.id)
    )
    if q:
        query = query.where(Patient.name.ilike(f"%{q}%"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(query.order_by(Session.date.desc()).offset((page - 1) * limit).limit(limit))
    items = [
        SessionGlobalResponse(
            id=str(s.id),
            appointment_id=str(s.appointment_id) if s.appointment_id else None,
            patient_id=str(p.id),
            patient_name=p.name,
            avatar_color=p.avatar_color,
            date=s.date.isoformat(),
            duration=s.duration,
            therapist=professional.name,
            type=s.type,
            objectives=s.objectives or [],
            notes=s.notes,
        )
        for s, p in result.all()
    ]
    return PaginatedResponse(items=items, total=total or 0, page=page, limit=limit)


patient_router = APIRouter(prefix="/patients/{patient_id}/sessions", tags=["sessions"])


@patient_router.get("")
async def list_patient_sessions(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(
        select(Session).where(Session.patient_id == patient.id).order_by(Session.date.desc())
    )
    return [
        {
            "id": str(s.id),
            "appointmentId": str(s.appointment_id) if s.appointment_id else None,
            "date": s.date.isoformat(),
            "duration": s.duration,
            "therapist": professional.name,
            "objectives": s.objectives or [],
            "notes": s.notes,
            "type": s.type,
        }
        for s in result.scalars().all()
    ]


@patient_router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    patient_id: UUID,
    body: SessionCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    if body.appointment_id:
        appointment_result = await db.execute(
            select(Appointment)
            .where(
                Appointment.id == body.appointment_id,
                Appointment.patient_id == patient.id,
                Appointment.professional_id == professional.id,
            )
            .with_for_update()
        )
        appointment = appointment_result.scalar_one_or_none()
        if not appointment:
            raise HTTPException(status_code=404, detail="Agendamento não encontrado para este paciente")
        if appointment.status == "cancelado":
            raise HTTPException(
                status_code=409,
                detail="Não é possível registrar sessão em um atendimento cancelado",
            )
        linked_result = await db.execute(
            select(Session.id).where(Session.appointment_id == appointment.id)
        )
        if linked_result.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail="Já existe uma sessão vinculada a este atendimento",
            )

    objectives = body.objectives
    if not objectives and db.bind and db.bind.dialect.name == "sqlite":
        objectives = literal_column("'[]'")
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        appointment_id=body.appointment_id,
        date=body.date or utcnow(),
        duration=body.duration,
        type=body.type,
        objectives=objectives,
        notes=body.notes,
    )
    db.add(session)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Já existe uma sessão vinculada a este atendimento",
        ) from exc
    await record_session(db, session=session, professional=professional)
    return {
        "id": str(session.id),
        "appointmentId": str(session.appointment_id) if session.appointment_id else None,
        "date": session.date.isoformat(),
        "duration": session.duration,
        "therapist": professional.name,
        "objectives": body.objectives,
        "notes": session.notes,
        "type": session.type,
    }


@patient_router.patch("/{session_id}")
async def update_session(
    patient_id: UUID,
    session_id: UUID,
    body: SessionUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(select(Session).where(Session.id == session_id, Session.patient_id == patient_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    await db.flush()
    return {"id": str(session.id), "message": "Atualizado"}


@patient_router.get("/{session_id}/evolutions")
async def list_session_evolutions(
    patient_id: UUID,
    session_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    session_result = await db.execute(select(Session).where(Session.id == session_id, Session.patient_id == patient_id))
    if session_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    result = await db.execute(
        select(Evolution)
        .where(Evolution.session_id == session_id, Evolution.patient_id == patient_id)
        .order_by(Evolution.date.desc())
    )
    return [
        {
            "id": str(e.id),
            "patientId": str(e.patient_id),
            "sessionId": str(e.session_id) if e.session_id else None,
            "date": e.date.isoformat(),
            "title": e.title,
            "content": e.content,
            "professional": professional.name,
        }
        for e in result.scalars().all()
    ]
