"""Durable and state-safe WhatsApp appointment event delivery."""

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.services.whatsapp_appointment_outbox import create_appointment_event_log
from app.services.evolution_whatsapp_service import EvolutionDeliveryUnknownError
from app.services.whatsapp_notification_service import WhatsAppNotificationService
from app.services.whatsapp_scheduler_service import WhatsAppSchedulerService
from app.services.whatsapp_types import WhatsAppSendResult


def _fake_provider(send_mock: AsyncMock):
    return SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=send_mock,
        send_appointment_reminder=send_mock,
    )


def _fake_provider_with_separate_methods(
    *, send_text_message: AsyncMock, send_appointment_reminder: AsyncMock
):
    return SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=send_text_message,
        send_appointment_reminder=send_appointment_reminder,
    )


async def _enable_event(db: AsyncSession, professional_id, event_id: str) -> None:
    db.add(
        NotificationSettings(
            professional_id=professional_id,
            whatsapp_enabled=True,
            whatsapp_events={event_id: True},
        )
    )
    await db.flush()


async def _enable_opt_in(db: AsyncSession, patient_id) -> None:
    caregiver = (
        await db.execute(select(Caregiver).where(Caregiver.patient_id == patient_id))
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"


def _appointment(professional_id, patient_id, *, slot_time=time(10, 0)) -> Appointment:
    return Appointment(
        professional_id=professional_id,
        patient_id=patient_id,
        date=date.today() + timedelta(days=1),
        time=slot_time,
        type="sessão",
        duration=50,
        status="confirmado",
    )


@pytest.mark.asyncio
async def test_queued_confirmation_is_superseded_after_cancellation(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_confirmation")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()
    event_log = await create_appointment_event_log(
        db_session, appointment, "confirmation"
    )
    await db_session.commit()

    appointment.status = "cancelado"
    await db_session.commit()

    send_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    sent = await WhatsAppNotificationService(db_session).dispatch_event_log(event_log.id)

    await db_session.refresh(event_log)
    assert sent is False
    assert event_log.status == "superseded"
    assert event_log.payload["skip_reason"] == "appointment_status_changed"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_latest_reschedule_event_is_dispatched(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_rescheduled")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()

    appointment.time = time(10, 30)
    await db_session.flush()
    old_event = await create_appointment_event_log(
        db_session, appointment, "rescheduled"
    )
    appointment.time = time(11, 0)
    await db_session.flush()
    current_event = await create_appointment_event_log(
        db_session, appointment, "rescheduled"
    )
    await db_session.commit()

    send_mock = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="latest-reschedule",
            status="sent",
            payload={},
        )
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    service = WhatsAppNotificationService(db_session)

    assert await service.dispatch_event_log(old_event.id) is False
    assert await service.dispatch_event_log(current_event.id) is True
    await db_session.refresh(old_event)
    assert old_event.status == "superseded"
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_reminder_rechecks_database_state_after_cron_capture(
    db_engine, db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_reminder_24h")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()
    event_log = await create_appointment_event_log(
        db_session,
        appointment,
        "appointment_reminder_24h",
        deduplication_key=(
            f"appointment-reminder-24h:{appointment.id}:"
            f"{appointment.date.isoformat()}:{appointment.time.isoformat()}"
        ),
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as concurrent_db:
        await concurrent_db.execute(
            update(Appointment)
            .where(Appointment.id == appointment.id)
            .values(status="cancelado")
        )
        await concurrent_db.commit()

    send_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    sent = await WhatsAppNotificationService(db_session).dispatch_event_log(event_log.id)

    await db_session.refresh(event_log)
    assert sent is False
    assert event_log.status == "superseded"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_keeps_existing_provider_path_when_response_link_is_disabled(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_reminder_24h")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()

    send_text = AsyncMock()
    send_default = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="default-reminder",
            status="sent",
            payload={},
        )
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider_with_separate_methods(
            send_text_message=send_text,
            send_appointment_reminder=send_default,
        ),
    )

    sent = await WhatsAppNotificationService(db_session).dispatch_appointment_reminder(
        appointment
    )

    assert sent is True
    send_default.assert_awaited_once()
    send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_in_reminder_appends_signed_response_link_at_dispatch_time(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_reminder_24h")
    notification_settings = (
        await db_session.execute(
            select(NotificationSettings).where(
                NotificationSettings.professional_id == professional.id
            )
        )
    ).scalar_one()
    notification_settings.appointment_confirmation_link_enabled = True
    notification_settings.whatsapp_message_templates = {
        "appointment_reminder_24h": "Lembrete atualizado para {{appointmentTime}}."
    }
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()

    monkeypatch.setattr(
        get_settings(), "frontend_url", "https://app.korusfono.com.br", raising=False
    )
    monkeypatch.setattr(
        "app.services.appointment_response_service.ZoneInfo", lambda _key: UTC
    )
    send_text = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="linked-reminder",
            status="sent",
            payload={},
        )
    )
    send_default = AsyncMock()
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider_with_separate_methods(
            send_text_message=send_text,
            send_appointment_reminder=send_default,
        ),
    )

    sent = await WhatsAppNotificationService(db_session).dispatch_appointment_reminder(
        appointment
    )

    assert sent is True
    send_default.assert_not_awaited()
    sent_text = send_text.await_args.args[2]
    assert sent_text.startswith("Lembrete atualizado para 10:00.")
    assert "Confirme ou cancele sua presença:" in sent_text
    assert "https://app.korusfono.com.br/confirmar-consulta#token=" in sent_text
    sent_token = sent_text.rsplit("#token=", maxsplit=1)[1]
    assert len(sent_token) == 44
    assert "." not in sent_token


@pytest.mark.asyncio
async def test_recent_previous_slot_reminder_suppresses_new_slot_reminder(
    db_session, professional, patient, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "whatsapp_reminder_reschedule_cooldown_hours", 6, raising=False
    )
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_reminder_24h")
    appointment = _appointment(professional.id, patient.id, slot_time=time(10, 5))
    db_session.add(appointment)
    await db_session.flush()
    db_session.add(
        NotificationMessageLog(
            professional_id=professional.id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            channel="whatsapp",
            notification_type="appointment_reminder_24h",
            provider="evolution",
            provider_message_id="previous-slot",
            deduplication_key=f"previous-slot:{appointment.id}",
            status="sent",
            scheduled_date=appointment.date,
            scheduled_time=time(10, 0),
            attempt_count=1,
            sent_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    event_log = await create_appointment_event_log(
        db_session,
        appointment,
        "appointment_reminder_24h",
        deduplication_key=f"new-slot:{appointment.id}",
    )
    await db_session.commit()

    send_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    sent = await WhatsAppNotificationService(db_session).dispatch_event_log(event_log.id)

    await db_session.refresh(event_log)
    assert sent is False
    assert event_log.status == "superseded"
    assert event_log.payload["skip_reason"] == "recent_reminder_for_previous_slot"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_recovers_queued_outbox_event(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_confirmation")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()
    event_log = await create_appointment_event_log(
        db_session, appointment, "confirmation"
    )
    await db_session.commit()

    send_mock = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="recovered-outbox",
            status="sent",
            payload={},
        )
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_scheduler_service.ZoneInfo", lambda _key: UTC
    )
    monkeypatch.setattr(get_settings(), "clinic_timezone", "UTC")

    recovered = await WhatsAppSchedulerService(db_session).run_queued_appointment_events()

    await db_session.refresh(event_log)
    assert recovered == 1
    assert event_log.status == "sent"
    assert event_log.provider_message_id == "recovered-outbox"


@pytest.mark.asyncio
async def test_ambiguous_delivery_is_not_retried_automatically(
    db_session, professional, patient, monkeypatch
):
    await _enable_opt_in(db_session, patient.id)
    await _enable_event(db_session, professional.id, "appointment_confirmation")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()
    event_log = await create_appointment_event_log(
        db_session, appointment, "confirmation"
    )
    await db_session.commit()

    send_mock = AsyncMock(
        side_effect=EvolutionDeliveryUnknownError("timeout após possível aceite")
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    service = WhatsAppNotificationService(db_session)

    assert await service.dispatch_event_log(event_log.id) is False
    await db_session.refresh(event_log)
    assert event_log.status == "failed"
    assert event_log.error_code == "delivery_unknown"
    assert event_log.next_retry_at is None

    monkeypatch.setattr(
        "app.services.whatsapp_scheduler_service.ZoneInfo", lambda _key: UTC
    )
    monkeypatch.setattr(get_settings(), "clinic_timezone", "UTC")
    recovered = await WhatsAppSchedulerService(db_session).run_queued_appointment_events()

    assert recovered == 0
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_stale_processing_is_quarantined_instead_of_retried(
    db_session, professional, patient, monkeypatch
):
    await _enable_event(db_session, professional.id, "appointment_confirmation")
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.flush()
    event_log = await create_appointment_event_log(
        db_session, appointment, "confirmation"
    )
    event_log.status = "processing"
    event_log.attempt_count = 1
    event_log.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()

    send_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: _fake_provider(send_mock),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_scheduler_service.ZoneInfo", lambda _key: UTC
    )
    monkeypatch.setattr(get_settings(), "clinic_timezone", "UTC")

    recovered = await WhatsAppSchedulerService(db_session).run_queued_appointment_events()

    await db_session.refresh(event_log)
    assert recovered == 0
    assert event_log.status == "failed"
    assert event_log.error_code == "delivery_unknown"
    send_mock.assert_not_awaited()
