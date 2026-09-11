"""Report deliveries: revocable links, WhatsApp/e-mail dispatch and public access."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.professional import Professional
from app.models.report_delivery import ReportDelivery
from app.services.whatsapp_types import WhatsAppSendResult


@pytest.fixture
async def report(db_session, professional, patient):
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="pais",
        date=date(2026, 9, 1),
        preview="Resumo do relatório",
        content="## Como está o(a) paciente\nJoão evoluiu bem.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


@pytest.fixture
async def caregiver(db_session, patient):
    return await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )


@pytest.fixture
def whatsapp_provider(monkeypatch):
    provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution",
                provider_message_id="delivery-msg-1",
                status="sent",
                payload={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.report_delivery_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    return provider


async def _create_delivery(api_client, auth_headers, report, body):
    return await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json=body,
    )


async def test_link_delivery_public_flow_and_counters(
    api_client, auth_headers, db_session, report
):
    response = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "created"
    assert data["url"] and "/relatorio/" in data["url"]
    assert data["snapshotMode"] == "fixed"
    assert data["reportVersion"] == 1
    assert data["contentHash"] == hashlib.sha256(report.content.encode("utf-8")).hexdigest()
    token = data["url"].rsplit("/", 1)[-1]

    stored = await db_session.scalar(
        select(ReportDelivery.token_hash).where(ReportDelivery.id == UUID(data["id"]))
    )
    assert stored is not None and stored != token and len(stored) == 64

    listing = await api_client.get(
        f"/api/v1/ai/reports/{report.id}/deliveries", headers=auth_headers
    )
    assert listing.status_code == 200
    assert listing.json()[0]["url"] is None

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200
    body = public.json()
    assert body["content"].startswith("## Como está")
    assert body["patientName"] == "João Silva"
    assert body["professionalName"] == "Dra. Teste"
    assert body["reportTypeLabel"] == "Relatório para Pais"
    assert body["snapshotMode"] == "fixed"
    assert body["reportVersion"] == 1
    assert body["contentHash"] == data["contentHash"]

    export = await api_client.get(
        f"/api/v1/report-deliveries/{token}/export", params={"format": "pdf"}
    )
    assert export.status_code == 200
    assert export.content.startswith(b"%PDF")

    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(data["id"]))
    )
    await db_session.refresh(row)
    assert row.view_count == 1
    assert row.download_count == 1
    assert row.first_viewed_at is not None
    assert row.last_downloaded_at is not None


async def test_delivery_requires_finalized_report(
    api_client, auth_headers, db_session, report
):
    report.status = "draft"
    await db_session.commit()
    response = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    assert response.status_code == 409
    assert "Finalize" in response.json()["detail"]


async def test_whatsapp_delivery_sends_message(
    api_client, auth_headers, db_session, report, caregiver, whatsapp_provider
):
    caregiver.whatsapp_opt_in = True
    await db_session.commit()
    response = await _create_delivery(
        api_client,
        auth_headers,
        report,
        {"channel": "whatsapp", "caregiverId": str(caregiver.id)},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "sent"
    assert "Maria Silva" in data["recipientLabel"]
    assert "11988887777" not in data["recipientLabel"]

    whatsapp_provider.send_text_message.assert_awaited_once()
    args = whatsapp_provider.send_text_message.await_args.args
    assert args[1] == "11988887777"
    assert "/relatorio/" in args[2]


async def test_whatsapp_delivery_requires_opt_in(
    api_client, auth_headers, db_session, report, caregiver, whatsapp_provider
):
    assert caregiver.whatsapp_opt_in is False
    response = await _create_delivery(
        api_client,
        auth_headers,
        report,
        {"channel": "whatsapp", "caregiverId": str(caregiver.id)},
    )
    assert response.status_code == 400
    assert "autorização" in response.json()["detail"]
    whatsapp_provider.send_text_message.assert_not_awaited()
    assert await db_session.scalar(select(ReportDelivery.id)) is None


async def test_whatsapp_delivery_requires_connection(
    api_client, auth_headers, db_session, report, caregiver, monkeypatch
):
    caregiver.whatsapp_opt_in = True
    await db_session.commit()
    provider = SimpleNamespace(can_send=AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.services.report_delivery_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    response = await _create_delivery(
        api_client,
        auth_headers,
        report,
        {"channel": "whatsapp", "caregiverId": str(caregiver.id)},
    )
    assert response.status_code == 409
    assert "Conecte o WhatsApp" in response.json()["detail"]


async def test_email_delivery_sends_via_resend(
    api_client, auth_headers, report, monkeypatch
):
    send = MagicMock(return_value="email-msg-1")
    monkeypatch.setattr("app.services.report_delivery_service.send_email", send)
    response = await _create_delivery(
        api_client,
        auth_headers,
        report,
        {"channel": "email", "email": "familia@example.com"},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "sent"
    assert data["recipientLabel"] == "familia@example.com"
    send.assert_called_once()
    assert send.call_args.kwargs["to_email"] == "familia@example.com"
    assert "/relatorio/" in send.call_args.kwargs["html"]


async def test_email_delivery_marks_failure_when_unavailable(
    api_client, auth_headers, report, monkeypatch
):
    monkeypatch.setattr(
        "app.services.report_delivery_service.send_email", MagicMock(return_value=None)
    )
    response = await _create_delivery(
        api_client,
        auth_headers,
        report,
        {"channel": "email", "email": "familia@example.com"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["lastError"]


async def test_revoke_and_expire_block_public_access(
    api_client, auth_headers, db_session, report
):
    created = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    delivery_id = created.json()["id"]
    token = created.json()["url"].rsplit("/", 1)[-1]

    revoke = await api_client.delete(
        f"/api/v1/ai/reports/{report.id}/deliveries/{delivery_id}", headers=auth_headers
    )
    assert revoke.status_code == 200
    assert revoke.json()["revokedAt"] is not None

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 410
    export = await api_client.get(f"/api/v1/report-deliveries/{token}/export")
    assert export.status_code == 410

    # Expired link
    other = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    other_token = other.json()["url"].rsplit("/", 1)[-1]
    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(other.json()["id"]))
    )
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    expired = await api_client.get(f"/api/v1/report-deliveries/{other_token}")
    assert expired.status_code == 410


async def test_unknown_token_returns_410(api_client):
    response = await api_client.get("/api/v1/report-deliveries/not-a-real-token")
    assert response.status_code == 410
    assert "inválido" in response.json()["detail"]


@pytest.mark.parametrize("subscription_status", ["trial_expired", "past_due", "canceled"])
async def test_revoke_remains_available_in_read_only_mode(
    api_client, auth_headers, db_session, professional, report, subscription_status
):
    created = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    assert created.status_code == 201
    delivery = created.json()
    token = delivery["url"].rsplit("/", 1)[-1]
    revoke_url = f"/api/v1/ai/reports/{report.id}/deliveries/{delivery['id']}"
    professional.subscription_status = subscription_status
    await db_session.commit()

    unauthenticated = await api_client.delete(revoke_url)
    assert unauthenticated.status_code == 401
    blocked_create = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    assert blocked_create.status_code == 403
    blocked_edit = await api_client.patch(
        f"/api/v1/ai/reports/{report.id}", headers=auth_headers, json={"content": "Novo conteúdo"}
    )
    assert blocked_edit.status_code == 403

    revoked = await api_client.delete(revoke_url, headers=auth_headers)
    assert revoked.status_code == 200
    assert revoked.json()["revokedAt"] is not None
    assert (await api_client.get(f"/api/v1/report-deliveries/{token}")).status_code == 410
    assert (await api_client.get(f"/api/v1/report-deliveries/{token}/export")).status_code == 410


async def test_deliveries_isolated_between_professionals(
    api_client, auth_headers, db_session, report
):
    other = Professional(
        email="other-delivery@example.com",
        password_hash=hash_password("testpass123"),
        name="Dr. Outro",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999990002",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    create = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=headers,
        json={"channel": "link"},
    )
    assert create.status_code == 404

    listing = await api_client.get(
        f"/api/v1/ai/reports/{report.id}/deliveries", headers=headers
    )
    assert listing.status_code == 404

    created = await _create_delivery(api_client, auth_headers, report, {"channel": "link"})
    delivery_id = created.json()["id"]
    other.subscription_status = "past_due"
    await db_session.commit()
    revoke = await api_client.delete(
        f"/api/v1/ai/reports/{report.id}/deliveries/{delivery_id}", headers=headers
    )
    assert revoke.status_code == 404
