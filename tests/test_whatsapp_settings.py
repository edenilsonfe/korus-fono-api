"""Regression tests for per-professional WhatsApp message settings."""

from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.notification_message_log import NotificationMessageLog
from app.services.whatsapp_notification_service import WhatsAppNotificationService
from app.services.whatsapp_types import WhatsAppSendResult


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    """Bind the write-entitlement middleware to the test database."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_reminder_template_survives_update_and_reload(api_client, auth_headers):
    custom_template = (
        "Oi, {{patientName}}! Seu atendimento com {{clinicianName}} será "
        "em {{appointmentDate}} às {{appointmentTime}}."
    )

    updated = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": custom_template,
            }
        },
    )

    assert updated.status_code == 200
    assert (
        updated.json()["whatsappMessageTemplates"]["appointment_reminder_24h"]
        == custom_template
    )
    assert updated.json()["templateDefaults"]["appointment_reminder_24h"]

    reloaded = await api_client.get(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
    )

    assert reloaded.status_code == 200
    assert (
        reloaded.json()["whatsappMessageTemplates"]["appointment_reminder_24h"]
        == custom_template
    )


@pytest.mark.asyncio
async def test_reminder_template_can_be_restored_to_default(api_client, auth_headers):
    await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": "Lembrete personalizado",
            }
        },
    )

    restored = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": None,
            }
        },
    )

    assert restored.status_code == 200
    assert restored.json()["whatsappMessageTemplates"]["appointment_reminder_24h"] is None
    assert restored.json()["templateDefaults"]["appointment_reminder_24h"]


@pytest.mark.asyncio
async def test_template_save_preserves_disabled_events_and_dispatches_only_enabled_ones(
    api_client,
    auth_headers,
    db_session: AsyncSession,
    professional,
    patient,
    monkeypatch,
):
    configured = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappEnabled": True,
            "whatsappEvents": {
                "appointmentReminder24h": True,
                "appointmentConfirmation": False,
                "appointmentCancelled": False,
                "appointmentRescheduled": True,
                "billingReminder": False,
                "billingOverdue": False,
            },
        },
    )
    assert configured.status_code == 200

    template_saved = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappEvents": {"appointmentReminder24h": True},
            "whatsappMessageTemplates": {
                "appointmentReminder24h": "Oi, {{patientName}}. Lembrete das {{appointmentTime}}.",
            },
        },
    )
    assert template_saved.status_code == 200

    reloaded = await api_client.get("/api/v1/whatsapp/settings", headers=auth_headers)
    assert reloaded.status_code == 200
    assert reloaded.json()["whatsappEvents"] == {
        "appointmentReminder24h": True,
        "appointmentConfirmation": False,
        "appointmentCancelled": False,
        "appointmentRescheduled": True,
        "billingReminder": False,
        "billingOverdue": False,
    }

    caregiver = (
        await db_session.execute(select(Caregiver).where(Caregiver.patient_id == patient.id))
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"

    event_ids = (
        "appointment_reminder_24h",
        "appointment_confirmation",
        "appointment_cancelled",
        "appointment_rescheduled",
    )
    appointments = []
    for offset in range(len(event_ids)):
        appointment = Appointment(
            professional_id=professional.id,
            patient_id=patient.id,
            date=date.today() + timedelta(days=offset + 1),
            time=time(9, 0),
            type="sessão",
            duration=50,
            status="confirmado",
        )
        db_session.add(appointment)
        appointments.append(appointment)
    await db_session.commit()

    send_mock = AsyncMock(
        side_effect=[
            WhatsAppSendResult(
                provider="evolution",
                provider_message_id="enabled-reminder",
                status="sent",
                payload={"ok": True},
            ),
            WhatsAppSendResult(
                provider="evolution",
                provider_message_id="enabled-rescheduled",
                status="sent",
                payload={"ok": True},
            ),
        ]
    )
    fake_provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=send_mock,
        send_appointment_reminder=send_mock,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: fake_provider,
    )

    notifier = WhatsAppNotificationService(db_session)
    results = [
        await notifier._dispatch_appointment_event(appointment.id, event_id)
        for appointment, event_id in zip(appointments, event_ids, strict=True)
    ]

    assert results == [True, False, False, True]
    assert send_mock.await_count == 2

    sent_logs = (
        (
            await db_session.execute(
                select(NotificationMessageLog).where(
                    NotificationMessageLog.professional_id == professional.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {log.notification_type for log in sent_logs} == {
        "appointment_reminder_24h",
        "appointment_rescheduled",
    }
    for log in sent_logs:
        assert log.payload["dispatch_decision"]["whatsapp_events"] == {
            "appointment_reminder_24h": True,
            "appointment_confirmation": False,
            "appointment_cancelled": False,
            "appointment_rescheduled": True,
            "billing_reminder": False,
            "billing_overdue": False,
        }
