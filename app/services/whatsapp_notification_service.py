"""Dispatch WhatsApp notifications for clinical events."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.constants.whatsapp_events import (
    APPOINTMENT_NOTIFICATION_EVENT_MAP,
    WHATSAPP_EVENT_REMINDER_24H,
    format_event_message,
    normalize_whatsapp_events,
    normalize_whatsapp_message_templates,
)
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.notification_message_log import (
    MESSAGE_STATUS_FAILED,
    MESSAGE_STATUS_PROCESSING,
    MESSAGE_STATUS_QUEUED,
    MESSAGE_STATUS_SENT,
    MESSAGE_STATUS_SKIPPED,
    MESSAGE_STATUS_SUPERSEDED,
    NotificationMessageLog,
)
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.evolution_whatsapp_service import (
    EvolutionDeliveryUnknownError,
    mask_phone,
)
from app.services.whatsapp_appointment_outbox import create_appointment_event_log
from app.services.whatsapp_provider import get_active_whatsapp_provider

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3
_DONE_STATUSES = frozenset(
    {"sent", "delivered", "read", MESSAGE_STATUS_SKIPPED, MESSAGE_STATUS_SUPERSEDED}
)
_NON_RETRYABLE_ERROR_CODES = frozenset(
    {"no_phone", "missing_phone", "invalid_phone", "delivery_unknown"}
)


async def _primary_caregiver_contact(
    db: AsyncSession, patient_id: UUID
) -> tuple[str | None, bool]:
    result = await db.execute(
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .order_by(Caregiver.is_primary.desc(), Caregiver.created_at.asc())
    )
    caregivers = result.scalars().all()
    primary = next((c for c in caregivers if c.is_primary), caregivers[0] if caregivers else None)
    if not primary:
        return None, False
    return primary.phone or None, primary.whatsapp_opt_in


class WhatsAppNotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_settings(self, professional_id: UUID) -> NotificationSettings | None:
        result = await self.db.execute(
            select(NotificationSettings).where(NotificationSettings.professional_id == professional_id)
        )
        return result.scalar_one_or_none()

    async def _event_allowed(
        self, professional_id: UUID, event_id: str
    ) -> tuple[bool, NotificationSettings | None]:
        settings = await self._get_settings(professional_id)
        if not settings or not settings.whatsapp_enabled:
            return False, settings

        events = normalize_whatsapp_events(settings.whatsapp_events)
        if not events.get(event_id):
            return False, settings

        provider = get_active_whatsapp_provider(self.db)
        if not await provider.can_send(professional_id):
            return False, settings

        return True, settings

    async def _find_idempotent_log(
        self,
        *,
        appointment_id: UUID,
        notification_type: str,
        scheduled_date: date | None,
        scheduled_time: time | None,
    ) -> NotificationMessageLog | None:
        result = await self.db.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.appointment_id == appointment_id,
                NotificationMessageLog.notification_type == notification_type,
                NotificationMessageLog.channel == "whatsapp",
                NotificationMessageLog.is_test.is_(False),
                NotificationMessageLog.scheduled_date == scheduled_date,
                NotificationMessageLog.scheduled_time == scheduled_time,
            )
        )
        return result.scalars().first()

    async def _claim_send_slot(
        self,
        *,
        professional_id: UUID,
        appointment_id: UUID,
        patient_id: UUID,
        notification_type: str,
        to_phone: str | None,
        provider: str,
        scheduled_date: date | None,
        scheduled_time: time | None,
        dispatch_decision: dict | None = None,
    ) -> NotificationMessageLog | None:
        """Insert or reclaim a log row before calling the provider. None = skip send."""
        existing = await self._find_idempotent_log(
            appointment_id=appointment_id,
            notification_type=notification_type,
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
        )
        if existing:
            if existing.status in _DONE_STATUSES:
                return None
            if existing.status == MESSAGE_STATUS_QUEUED:
                # Unknown outcome: never reclaim automatically, because the
                # provider may already have accepted the first request.
                return None
            if existing.status == MESSAGE_STATUS_FAILED:
                if existing.error_code in {"no_phone", "missing_phone", "invalid_phone"}:
                    return None
                if existing.attempt_count >= MAX_SEND_ATTEMPTS:
                    return None
                existing.status = MESSAGE_STATUS_QUEUED
                existing.attempt_count = int(existing.attempt_count or 0) + 1
                existing.last_error = None
                existing.error_code = None
                existing.failed_at = None
                existing.to_phone = mask_phone(to_phone) if to_phone else existing.to_phone
                if dispatch_decision is not None:
                    existing.payload = {"dispatch_decision": dispatch_decision}
                await self.db.commit()
                await self.db.refresh(existing)
                return existing
            return None

        log = NotificationMessageLog(
            id=uuid.uuid4(),
            professional_id=professional_id,
            appointment_id=appointment_id,
            patient_id=patient_id,
            channel="whatsapp",
            notification_type=notification_type,
            provider=provider,
            deduplication_key=(
                f"legacy-send-slot:{appointment_id}:{notification_type}:"
                f"{scheduled_date}:{scheduled_time}"
            ),
            to_phone=mask_phone(to_phone) if to_phone else None,
            status=MESSAGE_STATUS_QUEUED,
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
            attempt_count=1,
            payload=(
                {"dispatch_decision": dispatch_decision}
                if dispatch_decision is not None
                else None
            ),
        )
        self.db.add(log)
        try:
            await self.db.commit()
            await self.db.refresh(log)
            return log
        except IntegrityError:
            await self.db.rollback()
            raced = await self._find_idempotent_log(
                appointment_id=appointment_id,
                notification_type=notification_type,
                scheduled_date=scheduled_date,
                scheduled_time=scheduled_time,
            )
            if raced and raced.status == MESSAGE_STATUS_FAILED and raced.attempt_count < MAX_SEND_ATTEMPTS:
                return await self._claim_send_slot(
                    professional_id=professional_id,
                    appointment_id=appointment_id,
                    patient_id=patient_id,
                    notification_type=notification_type,
                    to_phone=to_phone,
                    provider=provider,
                    scheduled_date=scheduled_date,
                    scheduled_time=scheduled_time,
                    dispatch_decision=dispatch_decision,
                )
            return None

    async def _mark_log_sent(
        self,
        log: NotificationMessageLog,
        *,
        provider: str,
        provider_message_id: str | None,
        payload: dict | None,
        dispatch_decision: dict,
    ) -> None:
        log.status = MESSAGE_STATUS_SENT
        log.provider = provider
        log.provider_message_id = provider_message_id
        merged_payload = dict(log.payload or {})
        merged_payload.update(payload or {})
        merged_payload["dispatch_decision"] = dispatch_decision
        log.payload = merged_payload
        log.sent_at = datetime.now(UTC)
        log.failed_at = None
        log.last_error = None
        await self.db.commit()

    async def _mark_log_failed(
        self,
        log: NotificationMessageLog,
        *,
        error_code: str | None = None,
        last_error: str | None = None,
        payload: dict | None = None,
        dispatch_decision: dict,
    ) -> None:
        log.status = MESSAGE_STATUS_FAILED
        log.error_code = error_code
        log.last_error = last_error
        merged_payload = dict(log.payload or {})
        merged_payload.update(payload or {})
        merged_payload["dispatch_decision"] = dispatch_decision
        log.payload = merged_payload
        log.failed_at = datetime.now(UTC)
        if error_code not in _NON_RETRYABLE_ERROR_CODES and log.attempt_count < MAX_SEND_ATTEMPTS:
            delay_minutes = 5 * (3 ** max(0, log.attempt_count - 1))
            log.next_retry_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
        else:
            log.next_retry_at = None
        await self.db.commit()

    async def _mark_log_skipped(
        self,
        log: NotificationMessageLog,
        *,
        reason: str,
        superseded: bool = False,
    ) -> None:
        log.status = (
            MESSAGE_STATUS_SUPERSEDED if superseded else MESSAGE_STATUS_SKIPPED
        )
        payload = dict(log.payload or {})
        payload["skip_reason"] = reason
        log.payload = payload
        log.next_retry_at = None
        await self.db.commit()

    async def _create_failed_no_phone_log(
        self,
        *,
        professional_id: UUID,
        appointment_id: UUID,
        patient_id: UUID,
        notification_type: str,
        scheduled_date: date | None,
        scheduled_time: time | None,
        dispatch_decision: dict,
    ) -> None:
        existing = await self._find_idempotent_log(
            appointment_id=appointment_id,
            notification_type=notification_type,
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
        )
        if existing:
            return
        log = NotificationMessageLog(
            id=uuid.uuid4(),
            professional_id=professional_id,
            appointment_id=appointment_id,
            patient_id=patient_id,
            channel="whatsapp",
            notification_type=notification_type,
            provider=get_settings().whatsapp_provider,
            deduplication_key=(
                f"legacy-no-phone:{appointment_id}:{notification_type}:"
                f"{scheduled_date}:{scheduled_time}"
            ),
            to_phone=None,
            status=MESSAGE_STATUS_FAILED,
            error_code="no_phone",
            last_error="Responsável sem telefone cadastrado.",
            scheduled_date=scheduled_date,
            scheduled_time=scheduled_time,
            attempt_count=1,
            failed_at=datetime.now(UTC),
            payload={"dispatch_decision": dispatch_decision},
        )
        self.db.add(log)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()

    @staticmethod
    def _first_name(full_name: str | None) -> str:
        if not full_name or not full_name.strip():
            return ""
        return full_name.strip().split()[0]

    async def _claim_event_log(self, log_id: UUID) -> NotificationMessageLog | None:
        result = await self.db.execute(
            select(NotificationMessageLog)
            .where(NotificationMessageLog.id == log_id)
            .with_for_update(skip_locked=True)
        )
        log = result.scalar_one_or_none()
        if not log or log.status in _DONE_STATUSES:
            return None

        now = datetime.now(UTC)
        if log.status == MESSAGE_STATUS_QUEUED:
            if int(log.attempt_count or 0) != 0:
                return None
        elif log.status == MESSAGE_STATUS_FAILED:
            if log.error_code in _NON_RETRYABLE_ERROR_CODES:
                return None
            if int(log.attempt_count or 0) >= MAX_SEND_ATTEMPTS:
                return None
            if log.next_retry_at is not None:
                retry_at = log.next_retry_at
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                if retry_at > now:
                    return None
        else:
            return None

        log.status = MESSAGE_STATUS_PROCESSING
        log.attempt_count = int(log.attempt_count or 0) + 1
        log.next_retry_at = None
        log.last_error = None
        log.error_code = None
        await self.db.commit()
        await self.db.refresh(log)
        return log

    async def _current_appointment(self, appointment_id: UUID) -> Appointment | None:
        result = await self.db.execute(
            select(Appointment)
            .options(joinedload(Appointment.patient))
            .where(Appointment.id == appointment_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _snapshot_supersession_reason(
        appointment: Appointment,
        event_id: str,
        snapshot: dict,
    ) -> str | None:
        active_statuses = {"pendente", "confirmado"}
        expected_status = snapshot.get("appointment_status")
        if event_id == "appointment_confirmation":
            if expected_status != "confirmado" or appointment.status != "confirmado":
                return "appointment_status_changed"
        elif event_id == "appointment_cancelled":
            if expected_status != "cancelado" or appointment.status != "cancelado":
                return "appointment_status_changed"
        elif event_id in {"appointment_rescheduled", WHATSAPP_EVENT_REMINDER_24H}:
            if appointment.status not in active_statuses:
                return "appointment_status_changed"

        current_date = appointment.date.isoformat()
        current_time = appointment.time.isoformat()
        if snapshot.get("scheduled_date") != current_date:
            return "appointment_schedule_changed"
        if snapshot.get("scheduled_time") != current_time:
            return "appointment_schedule_changed"
        return None

    async def _has_recent_previous_slot_reminder(
        self, log: NotificationMessageLog
    ) -> bool:
        cooldown_hours = max(
            int(get_settings().whatsapp_reminder_reschedule_cooldown_hours), 0
        )
        if cooldown_hours == 0 or not log.appointment_id:
            return False
        cutoff = datetime.now(UTC) - timedelta(hours=cooldown_hours)
        result = await self.db.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.appointment_id == log.appointment_id,
                NotificationMessageLog.notification_type == WHATSAPP_EVENT_REMINDER_24H,
                NotificationMessageLog.id != log.id,
                NotificationMessageLog.status.in_(_DONE_STATUSES & {"sent", "delivered", "read"}),
                NotificationMessageLog.sent_at.is_not(None),
                NotificationMessageLog.sent_at >= cutoff,
            )
        )
        for previous in result.scalars().all():
            if (
                previous.scheduled_date != log.scheduled_date
                or previous.scheduled_time != log.scheduled_time
            ):
                return True
        return False

    async def dispatch_event_log(self, log_id: UUID) -> bool:
        """Claim and send one durable appointment outbox event."""
        log = await self._claim_event_log(log_id)
        if not log or not log.appointment_id:
            return False

        appointment = await self._current_appointment(log.appointment_id)
        if not appointment or not appointment.patient:
            await self._mark_log_skipped(
                log, reason="appointment_not_found", superseded=True
            )
            return False

        snapshot = (log.payload or {}).get("event_snapshot")
        if not isinstance(snapshot, dict):
            await self._mark_log_skipped(
                log, reason="missing_event_snapshot", superseded=True
            )
            return False
        supersession_reason = self._snapshot_supersession_reason(
            appointment, log.notification_type, snapshot
        )
        if supersession_reason:
            await self._mark_log_skipped(
                log, reason=supersession_reason, superseded=True
            )
            return False

        if (
            log.notification_type == WHATSAPP_EVENT_REMINDER_24H
            and await self._has_recent_previous_slot_reminder(log)
        ):
            await self._mark_log_skipped(
                log,
                reason="recent_reminder_for_previous_slot",
                superseded=True,
            )
            return False

        allowed, settings = await self._event_allowed(
            appointment.professional_id, log.notification_type
        )
        if not allowed or settings is None:
            await self._mark_log_skipped(
                log, reason="event_disabled_or_provider_unavailable"
            )
            return False

        events_snapshot = normalize_whatsapp_events(settings.whatsapp_events)
        dispatch_decision = {
            "whatsapp_enabled": settings.whatsapp_enabled,
            "whatsapp_events": events_snapshot,
            "settings_updated_at": (
                settings.updated_at.isoformat() if settings.updated_at is not None else None
            ),
        }

        patient: Patient = appointment.patient
        phone, opt_in = await _primary_caregiver_contact(self.db, patient.id)
        if not opt_in:
            await self._mark_log_skipped(log, reason="whatsapp_opt_in_missing")
            return False

        professional_result = await self.db.execute(
            select(Professional).where(Professional.id == appointment.professional_id)
        )
        professional = professional_result.scalar_one_or_none()
        if not professional:
            await self._mark_log_skipped(
                log, reason="professional_not_found", superseded=True
            )
            return False

        if not phone:
            await self._mark_log_failed(
                log,
                error_code="no_phone",
                last_error="Responsável sem telefone cadastrado.",
                dispatch_decision=dispatch_decision,
            )
            return False

        log.to_phone = mask_phone(phone)
        await self.db.commit()

        context = {
            "patient_name": patient.name,
            "patient_first_name": self._first_name(patient.name),
            "professional_name": professional.name,
            "professional_first_name": self._first_name(professional.name),
            "appointment_date": appointment.date.strftime("%d/%m/%Y"),
            "appointment_time": appointment.time.strftime("%H:%M"),
            "appointment_type": appointment.type or "sessão",
            "clinic_name": professional.name,
        }
        stored_templates = normalize_whatsapp_message_templates(
            settings.whatsapp_message_templates
        )
        custom_template = stored_templates.get(log.notification_type)

        try:
            provider = get_active_whatsapp_provider(self.db)
            if (
                log.notification_type == WHATSAPP_EVENT_REMINDER_24H
                and not custom_template
            ):
                variables = [
                    context["patient_name"],
                    context["professional_name"],
                    context["appointment_date"],
                    context["appointment_time"],
                    context["clinic_name"],
                ]
                send_result = await provider.send_appointment_reminder(
                    appointment.professional_id, phone, variables
                )
                payload = send_result.payload
            else:
                text = format_event_message(
                    log.notification_type,
                    context,
                    custom_template=custom_template,
                    stored_templates=stored_templates,
                )
                send_result = await provider.send_text_message(
                    appointment.professional_id, phone, text
                )
                payload = {"text": text, **(send_result.payload or {})}

            await self._mark_log_sent(
                log,
                provider=send_result.provider,
                provider_message_id=send_result.provider_message_id,
                payload=payload,
                dispatch_decision=dispatch_decision,
            )
            return True
        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp %s for appointment %s",
                log.notification_type,
                appointment.id,
            )
            try:
                await self.db.rollback()
                failed_log = await self.db.get(NotificationMessageLog, log.id)
                if failed_log is None:
                    return False
                unknown_delivery = isinstance(exc, EvolutionDeliveryUnknownError)
                await self._mark_log_failed(
                    failed_log,
                    error_code="delivery_unknown" if unknown_delivery else "provider_error",
                    last_error=str(getattr(exc, "detail", None) or exc),
                    payload={"error": str(exc)},
                    dispatch_decision=dispatch_decision,
                )
            except Exception:
                logger.exception(
                    "Failed to persist WhatsApp failure log for appointment %s",
                    appointment.id,
                )
            return False

    @staticmethod
    async def dispatch_appointment_event(appointment_id: UUID, notification_type: str) -> None:
        event_id = APPOINTMENT_NOTIFICATION_EVENT_MAP.get(notification_type)
        if not event_id:
            return

        async with AsyncSessionLocal() as db:
            service = WhatsAppNotificationService(db)
            await service._dispatch_appointment_event(appointment_id, event_id)

    async def dispatch_appointment_reminder(self, appointment: Appointment) -> bool:
        current = await self._current_appointment(appointment.id)
        if not current or not current.patient:
            return False
        deduplication_key = (
            f"appointment-reminder-24h:{current.id}:"
            f"{current.date.isoformat()}:{current.time.isoformat()}"
        )
        log = await create_appointment_event_log(
            self.db,
            current,
            WHATSAPP_EVENT_REMINDER_24H,
            deduplication_key=deduplication_key,
        )
        if not log:
            return False
        await self.db.commit()
        return await self.dispatch_event_log(log.id)

    async def _dispatch_appointment_event(
        self,
        appointment_id: UUID,
        event_id: str,
        *,
        appointment: Appointment | None = None,
    ) -> bool:
        current = await self._current_appointment(appointment_id)
        if not current or not current.patient:
            return False
        deduplication_key = (
            f"legacy-appointment-event:{current.id}:{event_id}:"
            f"{current.status}:{current.date.isoformat()}:{current.time.isoformat()}"
        )
        log = await create_appointment_event_log(
            self.db,
            current,
            event_id,
            deduplication_key=deduplication_key,
        )
        if not log:
            return False
        await self.db.commit()
        return await self.dispatch_event_log(log.id)
