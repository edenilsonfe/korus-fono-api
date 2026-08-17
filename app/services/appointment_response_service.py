"""Signed, public attendance responses for appointment reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.professional import Professional

APPOINTMENT_RESPONSE_TOKEN_TYPE = "appointment_attendance_response"
ACTIVE_RESPONSE_STATUSES = frozenset({"pendente", "confirmado"})
INVALID_LINK_MESSAGE = "Link inválido ou expirado."
UNAVAILABLE_RESPONSE_MESSAGE = (
    "Esta consulta não aceita mais respostas por este link."
)
CANCELLED_RESPONSE_MESSAGE = (
    "Esta consulta já foi cancelada e não pode ser reativada por este link."
)


class InvalidAppointmentResponseToken(ValueError):
    pass


class AppointmentResponseUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class AppointmentResponseDetails:
    appointment_date: str
    appointment_time: str
    professional_name: str
    status: str
    can_respond: bool


@dataclass(frozen=True)
class _ResponseTokenClaims:
    appointment_id: UUID
    professional_id: UUID
    scheduled_date: str
    scheduled_time: str


def _appointment_starts_at(appointment: Appointment) -> datetime:
    timezone = ZoneInfo(get_settings().clinic_timezone)
    return datetime.combine(appointment.date, appointment.time, tzinfo=timezone)


def create_appointment_response_token(appointment: Appointment) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(appointment.id),
        "type": APPOINTMENT_RESPONSE_TOKEN_TYPE,
        "professional_id": str(appointment.professional_id),
        "scheduled_date": appointment.date.isoformat(),
        "scheduled_time": appointment.time.isoformat(),
        "iat": now,
        "exp": _appointment_starts_at(appointment).astimezone(UTC),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def build_appointment_response_url(appointment: Appointment) -> str:
    settings = get_settings()
    base_url = (settings.frontend_url or "").strip().rstrip("/")
    token = quote(create_appointment_response_token(appointment), safe="")
    return f"{base_url}/confirmar-consulta#token={token}"


def _decode_response_token(token: str) -> _ResponseTokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={
                "require": [
                    "sub",
                    "type",
                    "professional_id",
                    "scheduled_date",
                    "scheduled_time",
                    "iat",
                    "exp",
                ]
            },
        )
        if payload.get("type") != APPOINTMENT_RESPONSE_TOKEN_TYPE:
            raise InvalidAppointmentResponseToken(INVALID_LINK_MESSAGE)
        return _ResponseTokenClaims(
            appointment_id=UUID(payload["sub"]),
            professional_id=UUID(payload["professional_id"]),
            scheduled_date=str(payload["scheduled_date"]),
            scheduled_time=str(payload["scheduled_time"]),
        )
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAppointmentResponseToken(INVALID_LINK_MESSAGE) from exc


async def _load_appointment(
    db: AsyncSession,
    claims: _ResponseTokenClaims,
    *,
    for_update: bool,
) -> tuple[Appointment, Professional]:
    statement = (
        select(Appointment, Professional)
        .join(Professional, Professional.id == Appointment.professional_id)
        .where(
            Appointment.id == claims.appointment_id,
            Appointment.professional_id == claims.professional_id,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=Appointment)
    result = await db.execute(statement)
    row = result.first()
    if row is None:
        raise InvalidAppointmentResponseToken(INVALID_LINK_MESSAGE)

    appointment, professional = row
    if (
        appointment.date.isoformat() != claims.scheduled_date
        or appointment.time.isoformat() != claims.scheduled_time
    ):
        raise InvalidAppointmentResponseToken(INVALID_LINK_MESSAGE)
    if _appointment_starts_at(appointment) <= datetime.now(
        ZoneInfo(get_settings().clinic_timezone)
    ):
        raise InvalidAppointmentResponseToken(INVALID_LINK_MESSAGE)
    return appointment, professional


def _details(
    appointment: Appointment, professional: Professional
) -> AppointmentResponseDetails:
    return AppointmentResponseDetails(
        appointment_date=appointment.date.isoformat(),
        appointment_time=appointment.time.strftime("%H:%M"),
        professional_name=professional.name,
        status=appointment.status,
        can_respond=appointment.status in ACTIVE_RESPONSE_STATUSES,
    )


async def preview_appointment_response(
    db: AsyncSession, token: str
) -> AppointmentResponseDetails:
    claims = _decode_response_token(token)
    appointment, professional = await _load_appointment(
        db, claims, for_update=False
    )
    return _details(appointment, professional)


async def submit_appointment_response(
    db: AsyncSession, token: str, action: str
) -> AppointmentResponseDetails:
    claims = _decode_response_token(token)
    appointment, professional = await _load_appointment(
        db, claims, for_update=True
    )

    if action == "confirm":
        if appointment.status == "cancelado":
            raise AppointmentResponseUnavailable(CANCELLED_RESPONSE_MESSAGE)
        if appointment.status not in ACTIVE_RESPONSE_STATUSES:
            raise AppointmentResponseUnavailable(UNAVAILABLE_RESPONSE_MESSAGE)
        appointment.status = "confirmado"
    elif action == "cancel":
        if appointment.status == "cancelado":
            return _details(appointment, professional)
        if appointment.status not in ACTIVE_RESPONSE_STATUSES:
            raise AppointmentResponseUnavailable(UNAVAILABLE_RESPONSE_MESSAGE)
        appointment.status = "cancelado"
    else:  # Pydantic rejects this at the HTTP boundary; keep service defensive.
        raise AppointmentResponseUnavailable(UNAVAILABLE_RESPONSE_MESSAGE)

    await db.flush()
    return _details(appointment, professional)
