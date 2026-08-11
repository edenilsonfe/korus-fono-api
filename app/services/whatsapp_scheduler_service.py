"""Scheduled WhatsApp jobs: 24h appointment reminders."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.constants.whatsapp_events import ACTIVE_APPOINTMENT_STATUSES, WHATSAPP_EVENT_REMINDER_24H
from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import (
    MESSAGE_STATUS_FAILED,
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
        reminders = await self.run_appointment_reminders_24h()
        welcome_messages = await retry_due_whatsapp_welcome_messages()
        totals = {
            "appointment_reminders": reminders,
            "welcome_messages": welcome_messages,
            "billing_reminders": 0,
            "billing_overdue": 0,
        }
        if any(totals.values()):
            logger.info("WhatsApp scheduler run: %s", totals)
        return totals

    async def run_appointment_reminders_24h(self) -> int:
        settings = get_settings()
        notifier = WhatsAppNotificationService(self.db)
        now = self._now()
        target = now + timedelta(hours=settings.whatsapp_reminder_window_hours)
        tolerance = timedelta(minutes=settings.whatsapp_reminder_tolerance_minutes)
        window_start = target - tolerance
        window_end = target + tolerance

        candidate_dates = {window_start.date(), window_end.date(), target.date()}

        result = await self.db.execute(
            select(Appointment)
            .options(joinedload(Appointment.patient))
            .where(
                Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
                Appointment.date.in_(candidate_dates),
            )
        )
        appointments = result.scalars().unique().all()
        candidates = []
        for appointment in appointments:
            starts_at = self._appointment_starts_at(appointment)
            if starts_at <= now:
                continue
            if not (window_start <= starts_at <= window_end):
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
            ).where(
                NotificationMessageLog.appointment_id.in_(appointment_ids),
                NotificationMessageLog.notification_type == WHATSAPP_EVENT_REMINDER_24H,
                NotificationMessageLog.channel == "whatsapp",
                NotificationMessageLog.is_test.is_(False),
                or_(
                    NotificationMessageLog.status.in_(
                        ("queued", "sent", "delivered", "read")
                    ),
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_FAILED,
                        NotificationMessageLog.attempt_count >= MAX_SEND_ATTEMPTS,
                    ),
                    NotificationMessageLog.error_code.in_(
                        ("no_phone", "missing_phone", "invalid_phone")
                    ),
                ),
            )
        )
        already_sent: set = set()
        by_id = {appointment.id: appointment for appointment in appointments}
        for appointment_id, scheduled_date, scheduled_time in result.all():
            appointment = by_id.get(appointment_id)
            if not appointment:
                continue
            if scheduled_date == appointment.date and scheduled_time == appointment.time:
                already_sent.add(appointment_id)
        return already_sent
