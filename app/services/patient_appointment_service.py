from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.services.whatsapp_appointment_outbox import create_appointment_event_logs

CANCELLABLE_APPOINTMENT_STATUSES = ("pendente", "confirmado")


def appointment_occurs_in_future(appointment: Appointment, clinic_now: datetime) -> bool:
    scheduled_at = datetime.combine(appointment.date, appointment.time).replace(
        tzinfo=clinic_now.tzinfo
    )
    return scheduled_at > clinic_now


async def cancel_future_patient_appointments(
    db: AsyncSession,
    *,
    professional_id: UUID,
    patient_id: UUID,
    clinic_now: datetime | None = None,
) -> tuple[list[Appointment], list[NotificationMessageLog]]:
    """Cancel eligible future appointments and persist their outbox events atomically."""
    if clinic_now is None:
        clinic_now = datetime.now(ZoneInfo(get_settings().clinic_timezone))

    result = await db.execute(
        select(Appointment)
        .where(
            Appointment.professional_id == professional_id,
            Appointment.patient_id == patient_id,
            Appointment.date >= clinic_now.date(),
            Appointment.status.in_(CANCELLABLE_APPOINTMENT_STATUSES),
        )
        .order_by(Appointment.date.asc(), Appointment.time.asc())
    )
    appointments = [
        appointment
        for appointment in result.scalars().all()
        if appointment_occurs_in_future(appointment, clinic_now)
    ]
    for appointment in appointments:
        appointment.status = "cancelado"

    event_logs = await create_appointment_event_logs(db, appointments, "cancelled")
    await db.commit()
    return appointments, event_logs
