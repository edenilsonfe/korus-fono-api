"""Scheduled WhatsApp jobs: 24h appointment reminders."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.constants.whatsapp_events import (
    ACTIVE_APPOINTMENT_STATUSES,
    WHATSAPP_EVENT_CANCELLED,
    WHATSAPP_EVENT_CONFIRMATION,
    WHATSAPP_EVENT_REMINDER_24H,
    WHATSAPP_EVENT_RESCHEDULED,
)
from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import (
    MESSAGE_STATUS_FAILED,
    MESSAGE_STATUS_PROCESSING,
    MESSAGE_STATUS_QUEUED,
    MESSAGE_STATUS_SKIPPED,
    NotificationMessageLog,
)
from app.services.whatsapp_notification_service import (
    MAX_SEND_ATTEMPTS,
    WhatsAppNotificationService,
)
from app.services.whatsapp_welcome_service import (
    retry_due_whatsapp_welcome_messages,
)

logger = logging.getLogger(__name__)

_APPOINTMENT_EVENT_TYPES = (
    WHATSAPP_EVENT_REMINDER_24H,
    WHATSAPP_EVENT_CONFIRMATION,
    WHATSAPP_EVENT_CANCELLED,
    WHATSAPP_EVENT_RESCHEDULED,
)
_NON_RETRYABLE_ERRORS = (
    "no_phone",
    "missing_phone",
    "invalid_phone",
    "delivery_unknown",
)


class WhatsAppSchedulerService:
    def __init__(self, db: AsyncSession):
        self.db = db
        settings = get_settings()
        self._tz = ZoneInfo(settings.clinic_timezone)

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    def _appointment_starts_at(self, appointment: Appointment) -> datetime:
        return datetime.combine(appointment.date, appointment.time, tzinfo=self._tz)

    async def run_all(self) -> dict[str, int]:
        appointment_events = await self.run_queued_appointment_events()
        reminders = await self.run_appointment_reminders_24h()
        welcome_messages = await retry_due_whatsapp_welcome_messages()
        totals = {
            "appointment_events": appointment_events,
            "appointment_reminders": reminders,
            "welcome_messages": welcome_messages,
            "billing_reminders": 0,
            "billing_overdue": 0,
        }
        if any(totals.values()):
            logger.info("WhatsApp scheduler run: %s", totals)
        return totals

    async def run_queued_appointment_events(self) -> int:
        """Recover durable outbox rows that background/ARQ did not finish."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(minutes=5)
        stale_result = await self.db.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.notification_type.in_(_APPOINTMENT_EVENT_TYPES),
                NotificationMessageLog.appointment_id.is_not(None),
                NotificationMessageLog.status == MESSAGE_STATUS_PROCESSING,
                NotificationMessageLog.updated_at < stale_before,
            )
        )
        stale_logs = stale_result.scalars().all()
        for log in stale_logs:
            log.status = MESSAGE_STATUS_FAILED
            log.error_code = "delivery_unknown"
            log.last_error = (
                "Processamento interrompido; entrega não repetida para evitar duplicidade."
            )
            log.failed_at = now
            log.next_retry_at = None
        if stale_logs:
            await self.db.commit()

        result = await self.db.execute(
            select(NotificationMessageLog.id)
            .where(
                NotificationMessageLog.notification_type.in_(_APPOINTMENT_EVENT_TYPES),
                NotificationMessageLog.appointment_id.is_not(None),
                or_(
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_QUEUED,
                        NotificationMessageLog.attempt_count == 0,
                    ),
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_FAILED,
                        NotificationMessageLog.attempt_count < MAX_SEND_ATTEMPTS,
                        or_(
                            NotificationMessageLog.error_code.is_(None),
                            NotificationMessageLog.error_code.notin_(_NON_RETRYABLE_ERRORS),
                        ),
                        or_(
                            NotificationMessageLog.next_retry_at.is_(None),
                            NotificationMessageLog.next_retry_at <= now,
                        ),
                    ),
                ),
            )
            .order_by(NotificationMessageLog.created_at.asc())
            .limit(100)
        )
        log_ids = list(result.scalars().all())
        notifier = WhatsAppNotificationService(self.db)
        sent = 0
        for log_id in log_ids:
            if await notifier.dispatch_event_log(log_id):
                sent += 1
        return sent

    async def run_appointment_reminders_24h(self) -> int:
        """Send unsent reminders as soon as an appointment enters the 24h horizon."""
        settings = get_settings()
        notifier = WhatsAppNotificationService(self.db)
        now = self._now()
        tolerance = timedelta(minutes=settings.whatsapp_reminder_tolerance_minutes)
        horizon_end = (
            now
            + timedelta(hours=settings.whatsapp_reminder_window_hours)
            + tolerance
        )

        result = await self.db.execute(
            select(Appointment)
            .options(joinedload(Appointment.patient))
            .where(
                Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
                Appointment.date >= now.date(),
                Appointment.date <= horizon_end.date(),
            )
        )
        appointments = result.scalars().unique().all()
        candidates = []
        for appointment in appointments:
            starts_at = self._appointment_starts_at(appointment)
            if starts_at <= now:
                continue
            if starts_at > horizon_end:
                continue
            candidates.append(appointment)

        already_sent_ids = await self._appointment_reminders_already_sent(candidates)
        sent = 0
        for appointment in candidates:
            if appointment.id in already_sent_ids:
                continue
            if await notifier.dispatch_appointment_reminder(appointment):
                sent += 1

        return sent

    async def _appointment_reminders_already_sent(
        self, appointments: list[Appointment]
    ) -> set:
        if not appointments:
            return set()

        appointment_ids = [appointment.id for appointment in appointments]
        result = await self.db.execute(
            select(
                NotificationMessageLog.appointment_id,
                NotificationMessageLog.scheduled_date,
                NotificationMessageLog.scheduled_time,
                NotificationMessageLog.status,
                NotificationMessageLog.payload,
            ).where(
                NotificationMessageLog.appointment_id.in_(appointment_ids),
                NotificationMessageLog.notification_type == WHATSAPP_EVENT_REMINDER_24H,
                NotificationMessageLog.channel == "whatsapp",
                NotificationMessageLog.is_test.is_(False),
                or_(
                    NotificationMessageLog.status.in_(
                        (
                            "queued",
                            "processing",
                            "sent",
                            "delivered",
                            "read",
                            "skipped",
                            "superseded",
                        )
                    ),
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_FAILED,
                        NotificationMessageLog.attempt_count >= MAX_SEND_ATTEMPTS,
                    ),
                    NotificationMessageLog.error_code.in_(
                        _NON_RETRYABLE_ERRORS
                    ),
                ),
            )
        )
        already_sent: set = set()
        by_id = {appointment.id: appointment for appointment in appointments}
        for (
            appointment_id,
            scheduled_date,
            scheduled_time,
            status,
            payload,
        ) in result.all():
            appointment = by_id.get(appointment_id)
            if not appointment:
                continue
            if scheduled_date == appointment.date and scheduled_time == appointment.time:
                if (
                    status == MESSAGE_STATUS_SKIPPED
                    and (payload or {}).get("skip_reason")
                    == "whatsapp_opt_in_missing"
                ):
                    continue
                already_sent.add(appointment_id)
        return already_sent
