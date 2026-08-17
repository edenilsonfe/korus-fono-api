from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.appointment_response import (
    AppointmentAttendanceResponse,
    AppointmentAttendanceResponseRequest,
    AppointmentResponseTokenRequest,
)
from app.services.appointment_response_service import (
    AppointmentResponseDetails,
    AppointmentResponseUnavailable,
    InvalidAppointmentResponseToken,
    preview_appointment_response,
    submit_appointment_response,
)

router = APIRouter(prefix="/appointment-responses", tags=["appointment-responses"])


def _response(details: AppointmentResponseDetails) -> AppointmentAttendanceResponse:
    return AppointmentAttendanceResponse(**details.__dict__)


@router.post("/preview", response_model=AppointmentAttendanceResponse)
async def preview_public_appointment_response(
    body: AppointmentResponseTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        details = await preview_appointment_response(db, body.token)
    except InvalidAppointmentResponseToken as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    return _response(details)


@router.post("", response_model=AppointmentAttendanceResponse)
async def submit_public_appointment_response(
    body: AppointmentAttendanceResponseRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        details = await submit_appointment_response(db, body.token, body.action)
    except InvalidAppointmentResponseToken as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except AppointmentResponseUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return _response(details)
