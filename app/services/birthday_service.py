"""Clinic-local birthdays shared by the dashboard and opt-in notifications."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import (
    WHATSAPP_EVENT_BIRTHDAY,
    normalize_whatsapp_events,
)
from app.core.config import get_settings
from app.models.app_notification import AppNotification
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient


def birthday_conditions(today: date):
    return (
        Patient.is_demo.is_(False),
        Patient.status != "inativo",
        Patient.birth_date <= today,
        func.extract("month", Patient.birth_date) == today.month,
        func.extract("day", Patient.birth_date) == today.day,
    )


async def birthday_reminder_enabled(db: AsyncSession, professional_id: UUID) -> bool:
    return bool(
        await db.scalar(
            select(NotificationSettings.birthday_in_app_enabled).where(
                NotificationSettings.professional_id == professional_id
            )
        )
    )


async def ensure_birthday_reminder(
    db: AsyncSession, professional_id: UUID, now: datetime
) -> bool:
    """Materialize one daily summary on inbox access; repeated reads keep seen/read state."""
    if not await birthday_reminder_enabled(db, professional_id):
        return False
    tz = ZoneInfo(get_settings().clinic_timezone)
    today = now.astimezone(tz).date()
    count = await db.scalar(
        select(func.count(Patient.id)).where(
            Patient.professional_id == professional_id, *birthday_conditions(today)
        )
    )
    notification_id = uuid5(NAMESPACE_URL, f"korus:birthday:{professional_id}:{today}")
    existing = await db.get(AppNotification, notification_id)
    if not count:
        if existing:
            existing.status = "archived"
            await db.flush()
        return True
    body = (
        "Você tem 1 paciente aniversariando hoje. Veja no dashboard."
        if count == 1
        else f"Você tem {count} pacientes aniversariando hoje. Veja no dashboard."
    )
    if existing:
        if existing.body != body or existing.status != "published":
            existing.body = body
            existing.status = "published"
            await db.flush()
        return True
    try:
        async with db.begin_nested():
            db.add(
                AppNotification(
                    id=notification_id,
                    kind="personal",
                    type="birthday",
                    title="Aniversariantes do dia",
                    body=body,
                    deep_link="/dashboard",
                    severity="info",
                    recipient_professional_id=professional_id,
                    status="published",
                    publish_at=datetime.combine(today, time.min, tzinfo=tz).astimezone(
                        UTC
                    ),
                    expires_at=datetime.combine(
                        today + timedelta(days=1), time.min, tzinfo=tz
                    ).astimezone(UTC),
                )
            )
            await db.flush()
    except IntegrityError:
        # The deterministic primary key protects concurrent inbox requests.
        if await db.get(AppNotification, notification_id) is None:
            raise
    return True


async def run_birthday_messages(db: AsyncSession, now: datetime) -> int:
    """Queue/recover today's messages, from 09:00 to 18:00 in clinic time."""
    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    local_now = now.astimezone(ZoneInfo(get_settings().clinic_timezone))
    today = local_now.date()
    await db.execute(
        update(NotificationMessageLog)
        .where(
            NotificationMessageLog.notification_type == WHATSAPP_EVENT_BIRTHDAY,
            NotificationMessageLog.status == "processing",
            NotificationMessageLog.updated_at
            < now.astimezone(UTC) - timedelta(minutes=5),
        )
        .values(
            status="failed",
            error_code="delivery_unknown",
            next_retry_at=None,
            failed_at=now.astimezone(UTC),
            last_error="Processamento interrompido; entrega não repetida para evitar duplicidade.",
        )
    )
    await db.execute(
        update(NotificationMessageLog)
        .where(
            NotificationMessageLog.notification_type == WHATSAPP_EVENT_BIRTHDAY,
            NotificationMessageLog.scheduled_date < today,
            NotificationMessageLog.status == "queued",
        )
        .values(
            status="superseded",
            next_retry_at=None,
            payload={"skip_reason": "birthday_expired"},
        )
    )
    await db.execute(
        update(NotificationMessageLog)
        .where(
            NotificationMessageLog.notification_type == WHATSAPP_EVENT_BIRTHDAY,
            NotificationMessageLog.scheduled_date < today,
            NotificationMessageLog.status == "failed",
        )
        .values(next_retry_at=None)
    )
    await db.commit()
    if not 9 <= local_now.hour < 18:
        return 0
    rows = (
        await db.execute(
            select(
                Patient.id,
                Patient.professional_id,
                NotificationSettings.whatsapp_events,
            )
            .join(
                NotificationSettings,
                NotificationSettings.professional_id == Patient.professional_id,
            )
            .where(
                NotificationSettings.whatsapp_enabled.is_(True),
                *birthday_conditions(today),
            )
        )
    ).all()
    for patient_id, professional_id, events in rows:
        if not normalize_whatsapp_events(events)[WHATSAPP_EVENT_BIRTHDAY]:
            continue
        key = f"birthday:{professional_id}:{patient_id}:{today}"
        if await db.scalar(
            select(NotificationMessageLog.id).where(
                NotificationMessageLog.deduplication_key == key
            )
        ):
            continue
        try:
            async with db.begin_nested():
                db.add(
                    NotificationMessageLog(
                        professional_id=professional_id,
                        patient_id=patient_id,
                        notification_type=WHATSAPP_EVENT_BIRTHDAY,
                        provider=get_settings().whatsapp_provider,
                        deduplication_key=key,
                        scheduled_date=today,
                        status="queued",
                        attempt_count=0,
                    )
                )
                await db.flush()
        except IntegrityError:
            if not await db.scalar(
                select(NotificationMessageLog.id).where(
                    NotificationMessageLog.deduplication_key == key
                )
            ):
                raise
    await db.commit()
    # Include rows whose patient/settings changed after queueing, so dispatch can
    # invalidate them. Claiming and retries use the existing durable outbox rules.
    ids = list(
        (
            await db.scalars(
                select(NotificationMessageLog.id).where(
                    NotificationMessageLog.notification_type == WHATSAPP_EVENT_BIRTHDAY,
                    NotificationMessageLog.scheduled_date == today,
                    NotificationMessageLog.status.in_(
                        ("queued", "failed", "processing")
                    ),
                    NotificationMessageLog.is_test.is_(False),
                )
            )
        ).all()
    )
    notifier = WhatsAppNotificationService(db)
    sent = 0
    for log_id in ids:
        if await notifier.dispatch_birthday_log(log_id, now=local_now):
            sent += 1
    return sent
