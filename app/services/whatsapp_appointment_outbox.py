"""Transactional outbox creation for WhatsApp appointment events."""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import (
    APPOINTMENT_NOTIFICATION_EVENT_MAP,
    WHATSAPP_EVENT_IDS,
    normalize_whatsapp_events,
)
from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import (
    MESSAGE_STATUS_QUEUED,
    NotificationMessageLog,
)
from app.models.notification_settings import NotificationSettings


def resolve_appointment_event_id(event_name: str) -> str | None:
    event_id = APPOINTMENT_NOTIFICATION_EVENT_MAP.get(event_name, event_name)
    return event_id if event_id in WHATSAPP_EVENT_IDS else None


def appointment_event_snapshot(appointment: Appointment) -> dict[str, str]:
    return {
        "appointment_status": appointment.status,
        "scheduled_date": appointment.date.isoformat(),
        "scheduled_time": appointment.time.isoformat(),
        "appointment_type": appointment.type,
    }


async def create_appointment_event_log(
    db: AsyncSession,
    appointment: Appointment,
    event_name: str,
    *,
    deduplication_key: str | None = None,
) -> NotificationMessageLog | None:
    """Persist an enabled appointment event in the current DB transaction."""
    event_id = resolve_appointment_event_id(event_name)
    if not event_id:
        return None

    settings_result = await db.execute(
        select(NotificationSettings).where(
            NotificationSettings.professional_id == appointment.professional_id
        )
    )
    notification_settings = settings_result.scalar_one_or_none()
    if not notification_settings or not notification_settings.whatsapp_enabled:
        return None
    if not normalize_whatsapp_events(notification_settings.whatsapp_events).get(event_id):
        return None

    if deduplication_key:
        existing_result = await db.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.deduplication_key == deduplication_key
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing:
            return existing

    log_id = uuid.uuid4()
    log = NotificationMessageLog(
        id=log_id,
        professional_id=appointment.professional_id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        channel="whatsapp",
        notification_type=event_id,
        provider=get_settings().whatsapp_provider,
        deduplication_key=deduplication_key or f"appointment-event:{log_id}",
        status=MESSAGE_STATUS_QUEUED,
        scheduled_date=appointment.date,
        scheduled_time=appointment.time,
        attempt_count=0,
        payload={"event_snapshot": appointment_event_snapshot(appointment)},
    )
    db.add(log)
    await db.flush()
    return log
