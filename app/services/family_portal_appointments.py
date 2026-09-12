"""F14 onda 2 — agenda projetada do portal da família (somente leitura).

Projeta SOMENTE linhas reais da agenda do dono para aquele paciente:
``pendente``/``confirmado``, do instante atual até 60 dias à frente,
convertidas pelo ``CLINIC_TIMEZONE``. Nada de serviço, preço, tipo livre,
série, notas ou ocorrências virtuais de recorrência; nenhum model/schema
clínico novo é criado — a projeção usa ``Appointment`` como está.

``appointmentsEnabled=false`` devolve o MESMO envelope com ``items=[]`` e
``total=0``, sem consulta clínica ampla.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.appointment import Appointment
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal_content import (
    PUBLIC_APPOINTMENT_LABEL,
    PublicFamilyPortalAppointment,
)
from app.services.family_portal_access import PublicFamilyPortalContext

PUBLIC_APPOINTMENT_STATUSES = ("pendente", "confirmado")
APPOINTMENT_HORIZON_DAYS = 60


def _public_appointment(
    appointment: Appointment, start: datetime, timezone: str
) -> PublicFamilyPortalAppointment:
    end = start + timedelta(minutes=appointment.duration or 0)
    return PublicFamilyPortalAppointment(
        id=str(appointment.id),
        label=PUBLIC_APPOINTMENT_LABEL,
        starts_at=start.astimezone(UTC),
        ends_at=end.astimezone(UTC),
        timezone=timezone,
        status=appointment.status,
    )


async def list_public_appointments(
    db: AsyncSession,
    context: PublicFamilyPortalContext,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[PublicFamilyPortalAppointment]:
    """GET /family-portal/appointments: corte [agora, agora + 60 dias]."""
    if not context.recipient.appointments_enabled:
        return PaginatedResponse(items=[], total=0, page=page, limit=limit)
    timezone = get_settings().clinic_timezone
    tz = ZoneInfo(timezone)
    now = utcnow()
    horizon = now + timedelta(days=APPOINTMENT_HORIZON_DAYS)
    # Pré-filtro pelo dia civil local; o corte exato (início/fim) é aplicado
    # logo abaixo com o fuso da clínica — sem carregar a agenda inteira.
    rows = (
        (
            await db.execute(
                select(Appointment)
                .where(
                    Appointment.patient_id == context.patient.id,
                    Appointment.professional_id
                    == context.portal.owner_professional_id,
                    Appointment.status.in_(PUBLIC_APPOINTMENT_STATUSES),
                    Appointment.date >= now.astimezone(tz).date(),
                    Appointment.date <= horizon.astimezone(tz).date(),
                )
                .order_by(
                    Appointment.date, Appointment.time, Appointment.id
                )
            )
        )
        .scalars()
        .all()
    )
    upcoming: list[tuple[datetime, Appointment]] = []
    for appointment in rows:
        start = datetime.combine(appointment.date, appointment.time, tzinfo=tz)
        if start < now or start > horizon:
            continue
        upcoming.append((start, appointment))
    upcoming.sort(key=lambda pair: (pair[0], str(pair[1].id)))
    total = len(upcoming)
    window = upcoming[(page - 1) * limit : (page - 1) * limit + limit]
    items = [
        _public_appointment(appointment, start, timezone)
        for start, appointment in window
    ]
    return PaginatedResponse(items=items, total=total, page=page, limit=limit)
