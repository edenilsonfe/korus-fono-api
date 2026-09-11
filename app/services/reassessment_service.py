"""Opt-in reassessment reminders for patients overdue for a follow-up assessment."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import (
    REASSESSMENT_DEFAULT_MONTHS,
    WHATSAPP_EVENT_REASSESSMENT,
    normalize_whatsapp_events,
)
from app.core.config import get_settings
from app.models.assessment import Assessment
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient


def months_ago(today: date, months: int) -> date:
    """Calendar-accurate `today - months`, clamping the day to the month end."""
    month_index = today.year * 12 + (today.month - 1) - months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(today.day, last_day))


def coerce_date(value) -> date | None:
    """SQLite returns dates as strings; Postgres returns date/datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _last_completed_subquery():
    return (
        select(
            Assessment.patient_id.label("patient_id"),
            func.max(Assessment.date).label("last_date"),
        )
        .where(Assessment.status == "completed")
        .group_by(Assessment.patient_id)
        .subquery()
    )


async def list_due_reassessments(
    db: AsyncSession,
    *,
    professional_id: UUID | None = None,
    today: date | None = None,
    only_whatsapp_enabled: bool = False,
) -> list[tuple[UUID, UUID, date, int]]:
    """Rows (patient_id, professional_id, last_assessment_date, months) overdue.

    A patient is due when active (not demo) and the newest completed assessment
    is older than the professional's configured reassessment window.
    """
    tz = ZoneInfo(get_settings().clinic_timezone)
    today = today or datetime.now(tz).date()
    last = _last_completed_subquery()
    query = (
        select(
            Patient.id,
            Patient.professional_id,
            last.c.last_date,
            NotificationSettings.reassessment_reminder_months,
            NotificationSettings.whatsapp_events,
        )
        .join(last, last.c.patient_id == Patient.id)
        .join(
            NotificationSettings,
            NotificationSettings.professional_id == Patient.professional_id,
        )
        .where(Patient.status == "ativo", Patient.is_demo.is_(False))
    )
    if professional_id is not None:
        query = query.where(Patient.professional_id == professional_id)
    if only_whatsapp_enabled:
        query = query.where(NotificationSettings.whatsapp_enabled.is_(True))
    rows = (await db.execute(query)).all()
    due: list[tuple[UUID, UUID, date, int]] = []
    for patient_id, prof_id, last_date, months, events in rows:
        effective_months = months or REASSESSMENT_DEFAULT_MONTHS
        last_date_value = coerce_date(last_date)
        if last_date_value is None or last_date_value > months_ago(today, effective_months):
            continue
        if only_whatsapp_enabled and not normalize_whatsapp_events(events)[
            WHATSAPP_EVENT_REASSESSMENT
        ]:
            continue
        due.append((patient_id, prof_id, last_date_value, effective_months))
    return due


async def count_due_reassessments(
    db: AsyncSession, professional_id: UUID, today: date | None = None
) -> int:
    return len(
        await list_due_reassessments(db, professional_id=professional_id, today=today)
    )


async def run_reassessment_messages(db: AsyncSession, now: datetime) -> int:
    """Queue and dispatch reassessment reminders; safe to run every 15 minutes."""
    local_now = now.astimezone(ZoneInfo(get_settings().clinic_timezone))
    today = local_now.date()
    await db.execute(
        update(NotificationMessageLog)
        .where(
            NotificationMessageLog.notification_type == WHATSAPP_EVENT_REASSESSMENT,
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
            NotificationMessageLog.notification_type == WHATSAPP_EVENT_REASSESSMENT,
            NotificationMessageLog.status == "failed",
        )
        .values(next_retry_at=None)
    )
    await db.commit()
    if not 9 <= local_now.hour < 18:
        return 0
    due = await list_due_reassessments(db, today=today, only_whatsapp_enabled=True)
    for patient_id, professional_id, last_date, _months in due:
        key = f"reassessment:{professional_id}:{patient_id}:{last_date.isoformat()}"
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
                        notification_type=WHATSAPP_EVENT_REASSESSMENT,
                        provider=get_settings().whatsapp_provider,
                        deduplication_key=key,
                        scheduled_date=today,
                        status="queued",
                        attempt_count=0,
                        payload={"last_assessment_date": last_date.isoformat()},
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

    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    ids = list(
        (
            await db.scalars(
                select(NotificationMessageLog.id).where(
                    NotificationMessageLog.notification_type
                    == WHATSAPP_EVENT_REASSESSMENT,
                    NotificationMessageLog.status.in_(("queued", "failed", "processing")),
                    NotificationMessageLog.is_test.is_(False),
                )
            )
        ).all()
    )
    notifier = WhatsAppNotificationService(db)
    sent = 0
    for log_id in ids:
        if await notifier.dispatch_reassessment_log(log_id, now=local_now):
            sent += 1
    return sent
