"""No-show policy: settings roundtrip and the 24h reminder notice."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.notification_settings import NotificationSettings
from app.services.whatsapp_notification_service import WhatsAppNotificationService
from app.services.whatsapp_types import WhatsAppSendResult

POLICY = "Faltas sem aviso prévio com menos de 4 horas podem ser cobradas."


@pytest.fixture
def clinic_clock(monkeypatch):
    monkeypatch.setattr(get_settings(), "clinic_timezone", "America/Sao_Paulo")


@pytest.fixture
async def reminder_setup(db_session, professional, patient):
    settings = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"appointment_reminder_24h": True},
        whatsapp_message_templates={},
        no_show_policy=POLICY,
    )
    db_session.add(settings)
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    caregiver.whatsapp_opt_in = True
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=__import__("datetime").time(10, 0),
        type="sessão",
        duration=50,
        status="pendente",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)
    return settings, appointment


@pytest.fixture
def whatsapp_provider(monkeypatch):
    provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution",
                provider_message_id="reminder-policy-1",
                status="sent",
                payload={},
            )
        ),
        send_appointment_reminder=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution",
                provider_message_id="default-path",
                status="sent",
                payload={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    return provider


async def test_reminder_includes_no_show_policy(
    db_session, reminder_setup, whatsapp_provider, clinic_clock
):
    _settings, appointment = reminder_setup
    service = WhatsAppNotificationService(db_session)
    sent = await service.dispatch_appointment_reminder(appointment)
    assert sent is True
    whatsapp_provider.send_text_message.assert_awaited_once()
    text = whatsapp_provider.send_text_message.await_args.args[2]
    assert POLICY in text
    assert "João" in text  # the regular reminder body is preserved
    whatsapp_provider.send_appointment_reminder.assert_not_awaited()


async def test_reminder_without_policy_uses_default_path(
    db_session, reminder_setup, whatsapp_provider, clinic_clock
):
    settings, appointment = reminder_setup
    settings.no_show_policy = None
    await db_session.commit()
    service = WhatsAppNotificationService(db_session)
    sent = await service.dispatch_appointment_reminder(appointment)
    assert sent is True
    whatsapp_provider.send_appointment_reminder.assert_awaited_once()
    whatsapp_provider.send_text_message.assert_not_awaited()


async def test_no_show_policy_settings_clearable(api_client, auth_headers, db_session):
    created = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={"noShowPolicy": POLICY},
    )
    assert created.status_code == 200
    assert created.json()["noShowPolicy"] == POLICY

    cleared = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={"noShowPolicy": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["noShowPolicy"] is None

    long = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={"noShowPolicy": "x" * 401},
    )
    assert long.status_code == 422
