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
    MESSAGE_STATUS_QUEUED,
    MESSAGE_STATUS_SENT,
    NotificationMessageLog,
)
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.evolution_whatsapp_service import mask_phone
from app.services.whatsapp_provider import get_active_whatsapp_provider

logger = logging.getLogger(__name__)

MAX_SEND_ATTEMPTS = 3
_DONE_STATUSES = frozenset({"sent", "delivered", "read"})
# ponytail: orphan reclaim if claim stayed queued with no provider id (post-send DB failure).
# Ceiling: concurrent double-send if two callers hit after 60s; upgrade = SELECT FOR UPDATE skip locked.
_QUEUED_ORPHAN_AFTER = timedelta(seconds=60)


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
                # In-flight claim: skip. Orphan after timeout (send OK, log commit failed): reclaim.
                if existing.provider_message_id:
                    return None
                stamped = existing.updated_at or existing.created_at
                if stamped is not None:
                    if stamped.tzinfo is None:
                        stamped = stamped.replace(tzinfo=UTC)
                    if datetime.now(UTC) - stamped < _QUEUED_ORPHAN_AFTER:
                        return None
                if existing.attempt_count >= MAX_SEND_ATTEMPTS:
                    return None
                existing.attempt_count = int(existing.attempt_count or 0) + 1
                existing.last_error = None
                existing.error_code = None
                existing.to_phone = mask_phone(to_phone) if to_phone else existing.to_phone
                if dispatch_decision is not None:
                    existing.payload = {"dispatch_decision": dispatch_decision}
                await self.db.commit()
                await self.db.refresh(existing)
                return existing
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
        log.payload = dict(payload or {})
        log.payload["dispatch_decision"] = dispatch_decision
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
        log.payload = dict(payload or {})
        log.payload["dispatch_decision"] = dispatch_decision
        log.failed_at = datetime.now(UTC)
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

    @staticmethod
    async def dispatch_appointment_event(appointment_id: UUID, notification_type: str) -> None:
        event_id = APPOINTMENT_NOTIFICATION_EVENT_MAP.get(notification_type)
        if not event_id:
            return

        async with AsyncSessionLocal() as db:
            service = WhatsAppNotificationService(db)
            await service._dispatch_appointment_event(appointment_id, event_id)

    async def dispatch_appointment_reminder(self, appointment: Appointment) -> bool:
        if not appointment.patient:
            return False
        return await self._dispatch_appointment_event(
            appointment.id,
            WHATSAPP_EVENT_REMINDER_24H,
            appointment=appointment,
        )

    async def _dispatch_appointment_event(
        self,
        appointment_id: UUID,
        event_id: str,
        *,
        appointment: Appointment | None = None,
    ) -> bool:
        if appointment is None:
            result = await self.db.execute(
                select(Appointment)
                .options(
                    joinedload(Appointment.patient),
                )
                .where(Appointment.id == appointment_id)
            )
            appointment = result.scalar_one_or_none()

        if not appointment or not appointment.patient:
            return False

        allowed, settings = await self._event_allowed(appointment.professional_id, event_id)
        if not allowed or settings is None:
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
            return False

        prof_result = await self.db.execute(
            select(Professional).where(Professional.id == appointment.professional_id)
        )
        professional = prof_result.scalar_one_or_none()
        if not professional:
            return False

        if not phone:
            await self._create_failed_no_phone_log(
                professional_id=appointment.professional_id,
                appointment_id=appointment.id,
                patient_id=patient.id,
                notification_type=event_id,
                scheduled_date=appointment.date,
                scheduled_time=appointment.time,
                dispatch_decision=dispatch_decision,
            )
            return False

        claim = await self._claim_send_slot(
            professional_id=appointment.professional_id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            notification_type=event_id,
            to_phone=phone,
            provider=get_settings().whatsapp_provider,
            scheduled_date=appointment.date,
            scheduled_time=appointment.time,
            dispatch_decision=dispatch_decision,
        )
        if claim is None:
            return False
        claim_id = claim.id

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
        custom_template = stored_templates.get(event_id)

        try:
            provider = get_active_whatsapp_provider(self.db)
            if event_id == WHATSAPP_EVENT_REMINDER_24H and not custom_template:
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
                    event_id,
                    context,
                    custom_template=custom_template,
                    stored_templates=stored_templates,
                )
                send_result = await provider.send_text_message(
                    appointment.professional_id, phone, text
                )
                payload = {"text": text, **(send_result.payload or {})}

            await self._mark_log_sent(
                claim,
                provider=send_result.provider,
                provider_message_id=send_result.provider_message_id,
                payload=payload,
                dispatch_decision=dispatch_decision,
            )
            return True
        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp %s for appointment %s",
                event_id,
                appointment_id,
            )
            try:
                await self.db.rollback()
                failed_claim = await self.db.get(NotificationMessageLog, claim_id)
                if failed_claim is None:
                    return False
                await self._mark_log_failed(
                    failed_claim,
                    last_error=str(getattr(exc, "detail", None) or exc),
                    payload={"error": str(exc)},
                    dispatch_decision=dispatch_decision,
                )
            except Exception:
                logger.exception(
                    "Failed to persist WhatsApp failure log for appointment %s",
                    appointment_id,
                )
                try:
                    await self.db.rollback()
                except Exception:
                    pass
            return False
