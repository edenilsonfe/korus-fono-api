"""Birthday preferences, tenant boundaries, clinic dates and durable delivery."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models.app_notification import AppNotification
from app.models.caregiver import Caregiver
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.models.professional import Professional
from app.services.birthday_service import (
    ensure_birthday_reminder,
    run_birthday_messages,
)
from app.services.evolution_whatsapp_service import EvolutionDeliveryUnknownError
from app.services.notification_service import (
    NotificationNotVisibleError,
    NotificationService,
)
from app.services.whatsapp_notification_service import WhatsAppNotificationService
from app.services.whatsapp_types import WhatsAppSendResult

TODAY = date(2026, 9, 3)
NOW = datetime(2026, 9, 3, 15, tzinfo=UTC)  # noon in Sao Paulo


@pytest.fixture
def clinic_clock(monkeypatch):
    monkeypatch.setattr(get_settings(), "clinic_timezone", "America/Sao_Paulo")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("app.services.notification_service.datetime", Clock)


@pytest.fixture
async def birthday_setup(db_session, professional, patient, monkeypatch, clinic_clock):
    patient.birth_date = date(2020, 9, 3)
    settings = NotificationSettings(
        professional_id=professional.id,
        birthday_in_app_enabled=True,
        whatsapp_enabled=True,
        whatsapp_events={"patient_birthday": True},
        whatsapp_message_templates={
            "patient_birthday": "Parabéns, {{nomePaciente}}! {{nomeProfissional}}"
        },
    )
    db_session.add(settings)
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    caregiver.whatsapp_opt_in = True
    await db_session.commit()
    provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution",
                provider_message_id="birthday-message",
                status="sent",
                payload={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    return settings, caregiver, provider


async def test_preferences_are_opt_in_persisted_and_independent(
    api_client, auth_headers, db_session, professional, monkeypatch
):
    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", session_factory)
    response = await api_client.get(
        "/api/v1/notifications/settings", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json() == {"birthdayInAppEnabled": False}
    response = await api_client.patch(
        "/api/v1/notifications/settings",
        headers=auth_headers,
        json={"birthdayInAppEnabled": True},
    )
    assert response.status_code == 200
    assert response.json() == {"birthdayInAppEnabled": True}
    whatsapp = await api_client.get("/api/v1/whatsapp/settings", headers=auth_headers)
    assert whatsapp.json()["whatsappEvents"]["patientBirthday"] is False
    response = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappEvents": {"patientBirthday": True},
            "whatsappMessageTemplates": {
                "patient_birthday": "Parabéns, {{nomePaciente}}!"
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["whatsappEvents"]["patientBirthday"] is True
    assert response.json()["whatsappMessageTemplates"]["patient_birthday"].startswith(
        "Parabéns"
    )
    settings = await db_session.scalar(
        select(NotificationSettings).where(
            NotificationSettings.professional_id == professional.id
        )
    )
    assert settings.birthday_in_app_enabled is True
    assert settings.whatsapp_enabled is False
    assert (
        await api_client.get("/api/v1/notifications/settings", headers=auth_headers)
    ).json() == {"birthdayInAppEnabled": True}


async def test_inbox_once_per_day_seen_read_and_broadcast_filter(
    db_session, professional, birthday_setup
):
    service = NotificationService(db_session)
    assert (await service.counts_for_professional(professional)).badge == 1
    page = await service.list_for_professional(professional=professional)
    item = page.items[0]
    assert item.type == "birthday" and item.kind == "personal"
    assert item.deep_link == "/dashboard"
    assert "1 paciente" in item.body
    assert (
        await service.list_for_professional(
            professional=professional, filter="broadcast"
        )
    ).items == []
    await service.mark_seen(professional)
    assert (await service.counts_for_professional(professional)).badge == 0
    from uuid import UUID

    await service.mark_read(professional, UUID(item.id))
    assert (await service.counts_for_professional(professional)).unread == 0
    assert await db_session.scalar(select(func.count(AppNotification.id))) == 1
    settings, _, _ = birthday_setup
    settings.birthday_in_app_enabled = False
    await db_session.flush()
    assert (await service.list_for_professional(professional=professional)).items == []
    with pytest.raises(NotificationNotVisibleError):
        await service.mark_read(professional, UUID(item.id))
    settings.birthday_in_app_enabled = True
    await db_session.flush()
    assert (await service.counts_for_professional(professional)).unread == 0


async def test_inbox_is_tenant_scoped_and_expires_at_clinic_midnight(
    db_session, professional, birthday_setup
):
    await ensure_birthday_reminder(db_session, professional.id, NOW)
    notification = await db_session.scalar(select(AppNotification))
    service = NotificationService(db_session)
    other = Professional(
        email="birthday-other@example.com",
        password_hash="x",
        name="Outro",
        specialty_key="fono",
        specialty="Fono",
        council="CRFa",
        phone="",
    )
    db_session.add(other)
    await db_session.flush()
    assert (await service.list_for_professional(professional=other)).items == []
    with pytest.raises(NotificationNotVisibleError):
        await service.mark_read(other, notification.id)
    assert service._is_visible_to(
        notification, professional, datetime(2026, 9, 4, 2, 59, tzinfo=UTC)
    )
    assert not service._is_visible_to(
        notification, professional, datetime(2026, 9, 4, 3, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "status,is_demo,birth_date",
    [
        ("inativo", False, date(2020, 9, 3)),
        ("ativo", True, date(2020, 9, 3)),
        ("ativo", False, date(2020, 9, 4)),
        ("ativo", False, date(2027, 9, 3)),
    ],
)
async def test_ineligible_patients_never_notify(
    db_session, professional, patient, birthday_setup, status, is_demo, birth_date
):
    patient.status, patient.is_demo, patient.birth_date = status, is_demo, birth_date
    await db_session.commit()
    assert (
        await NotificationService(db_session).counts_for_professional(professional)
    ).badge == 0
    assert await run_birthday_messages(db_session, NOW) == 0
    birthday_setup[2].send_text_message.assert_not_awaited()
    assert await db_session.scalar(select(func.count(NotificationMessageLog.id))) == 0


async def test_whatsapp_uses_current_template_and_sends_once(
    db_session, professional, patient, birthday_setup
):
    _, caregiver, provider = birthday_setup
    assert await run_birthday_messages(db_session, NOW) == 1
    assert await run_birthday_messages(db_session, NOW) == 0
    provider.send_text_message.assert_awaited_once_with(
        professional.id, caregiver.phone, "Parabéns, João! Dra."
    )
    log = await db_session.scalar(select(NotificationMessageLog))
    assert log.patient_id == patient.id and log.appointment_id is None
    assert log.scheduled_date == TODAY and log.status == "sent"
    assert "text" not in log.payload and caregiver.phone != log.to_phone


async def test_whatsapp_requires_consent(db_session, birthday_setup):
    _, caregiver, provider = birthday_setup
    caregiver.whatsapp_opt_in = False
    await db_session.commit()
    assert await run_birthday_messages(db_session, NOW) == 0
    provider.send_text_message.assert_not_awaited()
    log = await db_session.scalar(select(NotificationMessageLog))
    assert (
        log.status == "skipped"
        and log.payload["skip_reason"] == "whatsapp_opt_in_missing"
    )


@pytest.mark.parametrize("hour", [11, 21])  # 08h and 18h clinic time
async def test_whatsapp_respects_clinic_sending_window(
    db_session, birthday_setup, hour
):
    assert await run_birthday_messages(db_session, NOW.replace(hour=hour)) == 0
    birthday_setup[2].send_text_message.assert_not_awaited()
    assert await db_session.scalar(select(func.count(NotificationMessageLog.id))) == 0


async def test_connection_recovery_and_disable_before_send(db_session, birthday_setup):
    settings, _, provider = birthday_setup
    provider.can_send.return_value = False
    assert await run_birthday_messages(db_session, NOW) == 0
    log = await db_session.scalar(select(NotificationMessageLog))
    assert log.status == "queued" and log.attempt_count == 0
    settings.whatsapp_events = {"patient_birthday": False}
    await db_session.commit()
    provider.can_send.return_value = True
    assert await run_birthday_messages(db_session, NOW) == 0
    assert log.status == "skipped"
    provider.send_text_message.assert_not_awaited()


async def test_connection_recovers_same_day_without_requeue_duplicate(
    db_session, birthday_setup
):
    provider = birthday_setup[2]
    provider.can_send.return_value = False
    assert await run_birthday_messages(db_session, NOW) == 0
    provider.can_send.return_value = True
    assert await run_birthday_messages(db_session, NOW) == 1
    assert await db_session.scalar(select(func.count(NotificationMessageLog.id))) == 1


async def test_queue_rechecks_patient_and_never_sends_yesterdays_birthday(
    db_session, patient, birthday_setup
):
    provider = birthday_setup[2]
    provider.can_send.return_value = False
    await run_birthday_messages(db_session, NOW)
    log = await db_session.scalar(select(NotificationMessageLog))
    provider.can_send.return_value = True
    assert not await WhatsAppNotificationService(db_session).dispatch_birthday_log(
        log.id, now=NOW + timedelta(days=1)
    )
    assert log.status == "superseded"
    provider.send_text_message.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [EvolutionDeliveryUnknownError("uncertain"), RuntimeError("unexpected")]
)
async def test_unknown_delivery_is_not_retried(db_session, birthday_setup, error):
    provider = birthday_setup[2]
    provider.send_text_message.side_effect = error
    assert await run_birthday_messages(db_session, NOW) == 0
    assert await run_birthday_messages(db_session, NOW) == 0
    log = await db_session.scalar(select(NotificationMessageLog))
    assert log.error_code == "delivery_unknown" and log.next_retry_at is None
    assert provider.send_text_message.await_count == 1


async def test_clinic_date_and_leap_day(
    db_session, professional, patient, birthday_setup
):
    # UTC is already Sept 4, but the clinic is still celebrating Sept 3.
    assert await ensure_birthday_reminder(
        db_session, professional.id, datetime(2026, 9, 4, 1, tzinfo=UTC)
    )
    assert await db_session.scalar(select(func.count(AppNotification.id))) == 1
    patient.birth_date = date(2020, 2, 29)
    await db_session.commit()
    leap_day = datetime(2028, 2, 29, 12, tzinfo=ZoneInfo("America/Sao_Paulo"))
    assert await run_birthday_messages(db_session, leap_day) == 1
    assert (
        await run_birthday_messages(db_session, leap_day.replace(year=2027, day=28))
        == 0
    )


async def test_claim_reloads_state_changed_by_another_worker(
    db_session, birthday_setup
):
    birthday_setup[2].can_send.return_value = False
    await run_birthday_messages(db_session, NOW)
    cached_log = await db_session.scalar(select(NotificationMessageLog))
    await db_session.execute(
        update(NotificationMessageLog)
        .where(NotificationMessageLog.id == cached_log.id)
        .values(status="sent")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert cached_log.status == "queued"
    assert (
        await WhatsAppNotificationService(db_session)._claim_event_log(cached_log.id)
        is None
    )
    assert cached_log.status == "sent"


@pytest.mark.parametrize("status", ["queued", "processing"])
async def test_expired_or_interrupted_queue_never_resends(
    db_session, birthday_setup, status
):
    provider = birthday_setup[2]
    provider.can_send.return_value = False
    await run_birthday_messages(db_session, NOW)
    log = await db_session.scalar(select(NotificationMessageLog))
    log.status = status
    log.scheduled_date = TODAY - timedelta(days=1)
    log.updated_at = NOW - timedelta(days=1)
    await db_session.commit()
    # Disable new queueing while recovering the previous day's log.
    birthday_setup[0].whatsapp_enabled = False
    await db_session.commit()
    provider.can_send.return_value = True
    assert await run_birthday_messages(db_session, NOW) == 0
    await db_session.refresh(log)
    assert log.status == ("superseded" if status == "queued" else "failed")
    if status == "processing":
        assert log.error_code == "delivery_unknown"
    provider.send_text_message.assert_not_awaited()
