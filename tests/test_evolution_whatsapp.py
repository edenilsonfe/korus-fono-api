"""Tests for Evolution WhatsApp phone normalization, webhook security, and helpers."""

import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
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
from app.models.whatsapp_connection import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_NEEDS_RECONNECT,
    WhatsAppConnection,
)
from app.services.evolution_api_client import EvolutionApiClient, EvolutionApiError
from app.services.evolution_webhook_auth import (
    map_evolution_message_status,
    normalize_evolution_event,
    verify_evolution_webhook_request,
)
from app.services.evolution_whatsapp_service import (
    EvolutionWhatsAppService,
    format_reminder_text,
    mask_phone,
    normalize_whatsapp_number,
    whatsapp_number_candidates,
)
from app.services.whatsapp_notification_service import (
    MAX_SEND_ATTEMPTS,
    WhatsAppNotificationService,
)
from app.services.whatsapp_types import WhatsAppSendResult
from app.utils import credential_encryption as cred
from app.utils.credential_encryption import encrypt_secret


def test_normalize_whatsapp_number_mobile():
    assert normalize_whatsapp_number("11999990000") == "5511999990000"


def test_whatsapp_number_candidates_adds_ninth_digit():
    candidates = whatsapp_number_candidates("1188887777")
    assert "551188887777" in candidates
    assert any(len(c) == 13 for c in candidates)


def test_format_reminder_text():
    text = format_reminder_text(["Ana", "Dra. Silva", "10/07/2026", "14:00", "Clínica"])
    assert "Ana" in text
    assert "14:00" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [None, 500, 503])
async def test_send_text_does_not_retry_ambiguous_evolution_errors(status_code):
    client = EvolutionApiClient()
    client._request = AsyncMock(
        side_effect=EvolutionApiError("resultado da entrega desconhecido", status_code=status_code)
    )

    with pytest.raises(EvolutionApiError):
        await client.send_text("instance", "5511999999999", "mensagem", api_key="key")

    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_send_text_keeps_legacy_payload_fallback_for_schema_error():
    client = EvolutionApiClient()
    client._request = AsyncMock(
        side_effect=[
            EvolutionApiError("payload inválido", status_code=422),
            {"key": {"id": "legacy-shape"}},
        ]
    )

    result = await client.send_text(
        "instance", "5511999999999", "mensagem", api_key="key"
    )

    assert result["key"]["id"] == "legacy-shape"
    assert client._request.await_count == 2


def test_mask_phone():
    assert mask_phone("11999990000") == "1199***00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CONNECTION_UPDATE", "connection.update"),
        ("connection.update", "connection.update"),
        ("MESSAGES_UPDATE", "messages.update"),
        ("messages.update", "messages.update"),
        ("QRCODE_UPDATED", "qrcode.updated"),
        ("qrcode.updated", "qrcode.updated"),
        ("SEND_MESSAGE", "send.message"),
    ],
)
def test_normalize_evolution_event(raw, expected):
    assert normalize_evolution_event(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (3, "delivered"),
        (4, "read"),
        ("DELIVERY_ACK", "delivered"),
        ("READ", "read"),
        ("SERVER_ACK", "sent"),
    ],
)
def test_map_evolution_message_status(raw, expected):
    assert map_evolution_message_status(raw) == expected


@pytest.fixture
def evolution_env(monkeypatch):
    key = Fernet.generate_key().decode()
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "whatsapp_credential_encryption_key", key)
    monkeypatch.setattr(settings, "evolution_api_base_url", "http://evolution.test")
    monkeypatch.setattr(settings, "evolution_global_api_key", "global-key")
    monkeypatch.setattr(settings, "evolution_webhook_secret", "evo-webhook-secret")
    monkeypatch.setattr(settings, "app_public_url", "https://api.test")
    monkeypatch.setattr(settings, "debug", True)
    cred._get_fernet.cache_clear()
    yield
    cred._get_fernet.cache_clear()


@pytest.mark.asyncio
async def test_evolution_webhook_rejects_without_secret(evolution_env, api_client):
    response = await api_client.post(
        "/api/v1/webhooks/evolution/whatsapp",
        json={"event": "connection.update", "instance": "korus-test"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_evolution_webhook_accepts_bearer_secret(evolution_env, api_client):
    response = await api_client.post(
        "/api/v1/webhooks/evolution/whatsapp",
        json={"event": "connection.update", "instance": str(uuid.uuid4())},
        headers={"Authorization": "Bearer evo-webhook-secret"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_evolution_webhook_accepts_hmac_signature(evolution_env, api_client):
    body = b'{"event":"CONNECTION_UPDATE","instance":"korus-hmac"}'
    signature = hmac.new(b"evo-webhook-secret", body, hashlib.sha256).hexdigest()
    response = await api_client.post(
        "/api/v1/webhooks/evolution/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        },
    )
    assert response.status_code == 200


def test_verify_evolution_webhook_hmac(evolution_env):
    body = b'{"ok":true}'
    signature = hmac.new(b"evo-webhook-secret", body, hashlib.sha256).hexdigest()
    request = SimpleNamespace(
        headers={
            "X-Webhook-Signature": f"sha256={signature}",
        }
    )
    assert verify_evolution_webhook_request(request, body) is True


def test_instance_api_key_falls_back_to_global_on_decrypt_mismatch(evolution_env):
    """After WHATSAPP_CREDENTIAL_ENCRYPTION_KEY rotation, old ciphertext still sends via global key."""
    other = Fernet(Fernet.generate_key())
    connection = WhatsAppConnection(
        id=uuid.uuid4(),
        professional_id=uuid.uuid4(),
        provider="evolution",
        status=CONNECTION_STATUS_ACTIVE,
        evolution_instance_name="korus-test",
        encrypted_instance_api_key=other.encrypt(b"stale-instance-key").decode(),
    )
    service = EvolutionWhatsAppService(MagicMock())
    assert service._instance_api_key(connection) == "global-key"


@pytest.mark.asyncio
async def test_ensure_connection_open_fail_closed(evolution_env, db_session: AsyncSession):
    pro = Professional(
        email="evo-fail@example.com",
        password_hash=hash_password("x"),
        name="Pro",
        specialty_key="fono",
        specialty="Fono",
        council="CRFa",
        phone="11999990000",
    )
    db_session.add(pro)
    await db_session.flush()
    connection = WhatsAppConnection(
        professional_id=pro.id,
        provider="evolution",
        status=CONNECTION_STATUS_ACTIVE,
        evolution_instance_name="korus-test",
        encrypted_instance_api_key=encrypt_secret("instance-key"),
    )
    db_session.add(connection)
    await db_session.commit()

    client = MagicMock()
    client.connection_state = AsyncMock(
        side_effect=EvolutionApiError("down", status_code=503)
    )
    client.fetch_instances = AsyncMock(return_value=[])
    service = EvolutionWhatsAppService(db_session, client=client)

    with pytest.raises(Exception) as exc_info:
        await service._ensure_connection_open(connection)
    assert exc_info.value.status_code in (502, 409)
    await db_session.refresh(connection)
    assert connection.status == CONNECTION_STATUS_NEEDS_RECONNECT


@pytest.mark.asyncio
async def test_webhook_updates_delivery_timestamps(
    evolution_env, db_session: AsyncSession, professional: Professional
):
    connection = WhatsAppConnection(
        professional_id=professional.id,
        provider="evolution",
        status=CONNECTION_STATUS_ACTIVE,
        evolution_instance_name="korus-delivery",
        encrypted_instance_api_key=encrypt_secret("instance-key"),
    )
    log = NotificationMessageLog(
        professional_id=professional.id,
        channel="whatsapp",
        notification_type="appointment_confirmation",
        provider="evolution",
        provider_message_id="msg-1",
        status=MESSAGE_STATUS_SENT,
        sent_at=datetime.now(UTC),
        payload={
            "event_snapshot": {"appointment_status": "confirmado"},
            "dispatch_decision": {"whatsapp_enabled": True},
        },
    )
    db_session.add_all([connection, log])
    await db_session.commit()

    service = EvolutionWhatsAppService(db_session)
    await service.handle_webhook_event(
        {
            "event": "MESSAGES_UPDATE",
            "instance": "korus-delivery",
            "data": {"key": {"id": "msg-1"}, "status": 3},
        }
    )
    await db_session.refresh(log)
    assert log.status == "delivered"
    assert log.delivered_at is not None
    assert log.payload["event_snapshot"] == {"appointment_status": "confirmado"}
    assert log.payload["dispatch_decision"] == {"whatsapp_enabled": True}
    assert log.payload["provider_webhook"]["status"] == 3


@pytest.mark.asyncio
async def test_usage_counts_only_messages_accepted_by_provider(
    evolution_env, db_session: AsyncSession, professional: Professional
):
    db_session.add_all(
        [
            NotificationMessageLog(
                professional_id=professional.id,
                channel="whatsapp",
                notification_type="appointment_confirmation",
                provider="evolution",
                deduplication_key="usage-sent",
                status="sent",
                attempt_count=1,
            ),
            NotificationMessageLog(
                professional_id=professional.id,
                channel="whatsapp",
                notification_type="appointment_confirmation",
                provider="evolution",
                deduplication_key="usage-queued",
                status="queued",
                attempt_count=0,
            ),
            NotificationMessageLog(
                professional_id=professional.id,
                channel="whatsapp",
                notification_type="appointment_confirmation",
                provider="evolution",
                deduplication_key="usage-superseded",
                status="superseded",
                attempt_count=1,
            ),
        ]
    )
    await db_session.commit()

    usage = await EvolutionWhatsAppService(db_session).get_usage(professional.id)

    assert usage["used"] == 1


@pytest.mark.asyncio
async def test_claim_send_slot_blocks_duplicate(
    evolution_env, db_session: AsyncSession, professional: Professional, patient: Patient
):
    appt = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(14, 0),
        type="sessão",
        duration=50,
        status="agendado",
    )
    db_session.add(appt)
    await db_session.commit()

    service = WhatsAppNotificationService(db_session)
    first = await service._claim_send_slot(
        professional_id=professional.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        notification_type="appointment_confirmation",
        to_phone="11988887777",
        provider="evolution",
        scheduled_date=appt.date,
        scheduled_time=appt.time,
    )
    assert first is not None
    assert first.status == MESSAGE_STATUS_QUEUED
    assert first.to_phone == mask_phone("11988887777")

    second = await service._claim_send_slot(
        professional_id=professional.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        notification_type="appointment_confirmation",
        to_phone="11988887777",
        provider="evolution",
        scheduled_date=appt.date,
        scheduled_time=appt.time,
    )
    assert second is None


@pytest.mark.asyncio
async def test_claim_send_slot_retries_failed(
    evolution_env, db_session: AsyncSession, professional: Professional, patient: Patient
):
    appt = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=2),
        time=time(10, 0),
        type="sessão",
        duration=50,
        status="agendado",
    )
    db_session.add(appt)
    await db_session.flush()
    failed = NotificationMessageLog(
        professional_id=professional.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        channel="whatsapp",
        notification_type="appointment_confirmation",
        provider="evolution",
        status=MESSAGE_STATUS_FAILED,
        scheduled_date=appt.date,
        scheduled_time=appt.time,
        attempt_count=1,
        last_error="boom",
        is_test=False,
    )
    db_session.add(failed)
    await db_session.commit()

    service = WhatsAppNotificationService(db_session)
    reclaimed = await service._claim_send_slot(
        professional_id=professional.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        notification_type="appointment_confirmation",
        to_phone="11988887777",
        provider="evolution",
        scheduled_date=appt.date,
        scheduled_time=appt.time,
    )
    assert reclaimed is not None
    assert reclaimed.id == failed.id
    assert reclaimed.attempt_count == 2
    assert reclaimed.status == MESSAGE_STATUS_QUEUED

    reclaimed.status = MESSAGE_STATUS_FAILED
    reclaimed.attempt_count = MAX_SEND_ATTEMPTS
    await db_session.commit()
    exhausted = await service._claim_send_slot(
        professional_id=professional.id,
        appointment_id=appt.id,
        patient_id=patient.id,
        notification_type="appointment_confirmation",
        to_phone="11988887777",
        provider="evolution",
        scheduled_date=appt.date,
        scheduled_time=appt.time,
    )
    assert exhausted is None


@pytest.mark.asyncio
async def test_dispatch_uses_claim_before_send(
    evolution_env,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
    monkeypatch,
):
    caregiver = (
        await db_session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(Caregiver).where(
                Caregiver.patient_id == patient.id
            )
        )
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"

    settings_row = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"appointment_confirmation": True},
    )
    connection = WhatsAppConnection(
        professional_id=professional.id,
        provider="evolution",
        status=CONNECTION_STATUS_ACTIVE,
        evolution_instance_name="korus-send",
        encrypted_instance_api_key=encrypt_secret("k"),
    )
    appt = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=3),
        time=time(9, 0),
        type="sessão",
        duration=50,
        status="confirmado",
    )
    db_session.add_all([settings_row, connection, appt])
    await db_session.commit()
    await db_session.refresh(appt)

    send_mock = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="m1",
            status="sent",
            payload={"ok": True},
        )
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

    service = WhatsAppNotificationService(db_session)
    ok = await service._dispatch_appointment_event(appt.id, "appointment_confirmation")
    assert ok is True
    assert send_mock.await_count == 1

    log = (
        await db_session.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.appointment_id == appt.id,
                NotificationMessageLog.notification_type == "appointment_confirmation",
            )
        )
    ).scalar_one()
    assert log.payload["dispatch_decision"] == {
        "whatsapp_enabled": True,
        "appointment_confirmation_link_enabled": False,
        "whatsapp_events": {
            "appointment_reminder_24h": False,
            "appointment_confirmation": True,
            "appointment_cancelled": False,
            "appointment_rescheduled": False,
            "billing_reminder": False,
            "billing_overdue": False,
        },
        "settings_updated_at": settings_row.updated_at.isoformat(),
    }

    ok_again = await service._dispatch_appointment_event(appt.id, "appointment_confirmation")
    assert ok_again is False
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_failed_dispatch_preserves_decision_snapshot_after_rollback(
    evolution_env,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
    monkeypatch,
):
    caregiver = (
        await db_session.execute(select(Caregiver).where(Caregiver.patient_id == patient.id))
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"

    settings_row = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"appointment_confirmation": True},
    )
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=4),
        time=time(11, 0),
        type="sessão",
        duration=50,
        status="confirmado",
    )
    db_session.add_all([settings_row, appointment])
    await db_session.commit()
    appointment_id = appointment.id

    fake_provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(side_effect=RuntimeError("Evolution indisponível")),
        send_appointment_reminder=AsyncMock(side_effect=RuntimeError("Evolution indisponível")),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: fake_provider,
    )

    sent = await WhatsAppNotificationService(db_session)._dispatch_appointment_event(
        appointment_id, "appointment_confirmation"
    )

    assert sent is False
    log = (
        await db_session.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.appointment_id == appointment_id
            )
        )
    ).scalar_one()
    assert log.status == MESSAGE_STATUS_FAILED
    assert log.last_error == "Evolution indisponível"
    assert log.payload["dispatch_decision"]["whatsapp_events"][
        "appointment_confirmation"
    ] is True


@pytest.mark.asyncio
async def test_dispatch_reminder_uses_professional_custom_template(
    evolution_env,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
    monkeypatch,
):
    caregiver = (
        await db_session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(Caregiver).where(
                Caregiver.patient_id == patient.id
            )
        )
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"

    custom_template = (
        "Oi, {{patientName}}! Lembrete com {{clinicianName}} em "
        "{{appointmentDate}} às {{appointmentTime}}."
    )
    settings_row = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"appointment_reminder_24h": True},
        whatsapp_message_templates={"appointment_reminder_24h": custom_template},
    )
    appt = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(9, 0),
        type="sessão",
        duration=50,
        status="confirmado",
    )
    db_session.add_all([settings_row, appt])
    await db_session.commit()
    await db_session.refresh(appt)

    send_text_mock = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="custom-reminder-1",
            status="sent",
            payload={"ok": True},
        )
    )
    send_default_reminder_mock = AsyncMock()
    fake_provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=send_text_mock,
        send_appointment_reminder=send_default_reminder_mock,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: fake_provider,
    )

    ok = await WhatsAppNotificationService(db_session)._dispatch_appointment_event(
        appt.id, "appointment_reminder_24h"
    )

    assert ok is True
    send_text_mock.assert_awaited_once_with(
        professional.id,
        "11988887777",
        f"Oi, João! Lembrete com Dra. em {appt.date.strftime('%d/%m/%Y')} às 09:00.",
    )
    send_default_reminder_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_reminder_without_custom_template_keeps_provider_default(
    evolution_env,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
    monkeypatch,
):
    caregiver = (
        await db_session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(Caregiver).where(
                Caregiver.patient_id == patient.id
            )
        )
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    caregiver.phone = "11988887777"

    settings_row = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"appointment_reminder_24h": True},
        whatsapp_message_templates={},
    )
    appt = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(9, 0),
        type="sessão",
        duration=50,
        status="confirmado",
    )
    db_session.add_all([settings_row, appt])
    await db_session.commit()
    await db_session.refresh(appt)

    send_text_mock = AsyncMock()
    send_default_reminder_mock = AsyncMock(
        return_value=WhatsAppSendResult(
            provider="evolution",
            provider_message_id="default-reminder-1",
            status="sent",
            payload={"ok": True},
        )
    )
    fake_provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=send_text_mock,
        send_appointment_reminder=send_default_reminder_mock,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda _db: fake_provider,
    )

    ok = await WhatsAppNotificationService(db_session)._dispatch_appointment_event(
        appt.id, "appointment_reminder_24h"
    )

    assert ok is True
    send_default_reminder_mock.assert_awaited_once_with(
        professional.id,
        "11988887777",
        ["João Silva", "Dra. Teste", appt.date.strftime("%d/%m/%Y"), "09:00", "Dra. Teste"],
    )
    send_text_mock.assert_not_awaited()
