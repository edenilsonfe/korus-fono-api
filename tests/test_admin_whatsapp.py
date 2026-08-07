"""Admin panel tests for the platform WhatsApp connection (welcome messages)."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.whatsapp_connection import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_NOT_CONNECTED,
)
from app.services.evolution_api_client import EvolutionApiClient, EvolutionApiError
from app.utils import credential_encryption as cred
from app.utils.credential_encryption import encrypt_secret


class _FakeEvolutionClient:
    """Drop-in replacement for EvolutionApiClient used by the service."""

    extract_qrcode_base64 = staticmethod(EvolutionApiClient.extract_qrcode_base64)
    extract_instance_api_key = staticmethod(EvolutionApiClient.extract_instance_api_key)
    extract_connection_state = staticmethod(EvolutionApiClient.extract_connection_state)

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.created_instances: list[str] = []
        self.deleted_instances: list[str] = []

    async def create_instance(
        self, instance_name, *, qrcode=True, webhook_url=None, webhook_secret=None
    ):
        self.created_instances.append(instance_name)
        return {
            "instance": {"instanceName": instance_name},
            "hash": "inst-key",
            "qrcode": {"base64": "qr-plataforma"},
        }

    async def connect_instance(self, instance_name, *, api_key):
        return {"qrcode": {"base64": "qr-plataforma"}}

    async def connection_state(self, instance_name, *, api_key):
        return {"instance": {"state": "open"}}

    async def fetch_instances(self, instance_name=None, *, api_key=None):
        return [
            {
                "instanceName": instance_name,
                "number": "5511999998888",
                "ownerJid": "5511999998888@s.whatsapp.net",
            }
        ]

    async def logout_instance(self, instance_name, *, api_key):
        return {}

    async def delete_instance(self, instance_name, *, api_key):
        self.deleted_instances.append(instance_name)
        return {}

    async def set_webhook(self, instance_name, webhook_url, *, api_key, secret=None):
        return {}

    async def check_whatsapp_numbers(self, instance_name, numbers, *, api_key):
        return [
            {"number": n, "exists": True, "jid": f"{n}@s.whatsapp.net"}
            for n in numbers
        ]

    async def send_text(self, instance_name, number, text, *, api_key):
        return {"key": {"id": "msg-1"}}


class _ConnectingEvolutionClient(_FakeEvolutionClient):
    async def connection_state(self, instance_name, *, api_key):
        return {"instance": {"state": "connecting"}}

    async def connect_instance(self, instance_name, *, api_key):
        return {"qrcode": {"base64": "qr-renovado"}}


class _ExistingPlatformEvolutionClient(_FakeEvolutionClient):
    async def create_instance(
        self, instance_name, *, qrcode=True, webhook_url=None, webhook_secret=None
    ):
        raise EvolutionApiError("Instance already exists", status_code=403)


@pytest.fixture
def admin_evolution_env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_provider", "evolution")
    monkeypatch.setattr(settings, "whatsapp_credential_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "evolution_api_base_url", "http://evolution.test")
    monkeypatch.setattr(settings, "evolution_global_api_key", "global-key")
    monkeypatch.setattr(settings, "evolution_welcome_instance_name", "korus-welcome")
    monkeypatch.setattr(settings, "app_public_url", "https://api.test")
    monkeypatch.setattr(settings, "evolution_webhook_secret", "evo-secret")
    cred._get_fernet.cache_clear()
    yield settings
    cred._get_fernet.cache_clear()


@pytest.fixture
async def staff_professional(db_session, professional):
    professional.is_staff = True
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


@pytest.fixture
def staff_headers(staff_professional):
    token = create_access_token(staff_professional.id)
    return {"Authorization": f"Bearer {token}"}


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr(
        "app.services.platform_whatsapp_service.EvolutionApiClient",
        lambda: fake,
    )
    return fake


async def test_admin_status_requires_auth(api_client):
    response = await api_client.get("/api/v1/admin/whatsapp/platform")
    assert response.status_code == 401


async def test_admin_status_requires_staff(api_client, auth_headers):
    response = await api_client.get(
        "/api/v1/admin/whatsapp/platform", headers=auth_headers
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_status_not_connected(
    api_client, staff_headers, admin_evolution_env
):
    response = await api_client.get("/api/v1/admin/whatsapp/platform", headers=staff_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "evolution"
    assert body["connection"]["status"] == CONNECTION_STATUS_NOT_CONNECTED
    assert body["canSend"] is False


@pytest.mark.asyncio
async def test_admin_connect_creates_instance_and_returns_qr(
    api_client, staff_headers, admin_evolution_env, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/connect", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["qrcodeBase64"] == "qr-plataforma"
    assert body["connection"]["qrcodeBase64"] == "qr-plataforma"
    assert body["connection"]["status"] == CONNECTION_STATUS_ACTIVE
    assert body["connection"]["evolutionInstanceName"] == "korus-welcome"
    assert body["canSend"] is True
    assert fake.created_instances == ["korus-welcome"]


@pytest.mark.asyncio
async def test_admin_connect_uses_stable_platform_instance_name_by_default(
    api_client, staff_headers, admin_evolution_env, monkeypatch
):
    monkeypatch.setattr(admin_evolution_env, "evolution_welcome_instance_name", "")
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/connect", headers=staff_headers
    )

    assert response.status_code == 200
    assert response.json()["connection"]["evolutionInstanceName"] == "korus-welcome"
    assert fake.created_instances == ["korus-welcome"]


@pytest.mark.asyncio
async def test_admin_connect_reuses_stored_instance(
    api_client, staff_headers, admin_evolution_env, db_session, monkeypatch
):
    db_session.add(
        PlatformWhatsAppConnection(
            status=CONNECTION_STATUS_CONNECTING,
            evolution_instance_name="korus-custom",
            encrypted_instance_api_key=encrypt_secret("inst-key"),
        )
    )
    await db_session.commit()
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/connect", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["evolutionInstanceName"] == "korus-custom"
    assert body["connection"]["status"] == CONNECTION_STATUS_ACTIVE
    assert fake.created_instances == []


@pytest.mark.asyncio
async def test_admin_connect_adopts_existing_platform_instance_without_deleting_it(
    api_client, staff_headers, admin_evolution_env, monkeypatch
):
    fake = _install_fake(monkeypatch, _ExistingPlatformEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/connect", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["evolutionInstanceName"] == "korus-welcome"
    assert body["connection"]["status"] == CONNECTION_STATUS_ACTIVE
    assert body["canSend"] is True
    assert fake.deleted_instances == []


@pytest.mark.asyncio
async def test_admin_refresh_connection(
    api_client, staff_headers, admin_evolution_env, db_session, monkeypatch
):
    db_session.add(
        PlatformWhatsAppConnection(
            status=CONNECTION_STATUS_ACTIVE,
            evolution_instance_name="korus-welcome",
            encrypted_instance_api_key=encrypt_secret("inst-key"),
        )
    )
    await db_session.commit()
    _install_fake(monkeypatch, _FakeEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/refresh-connection", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["status"] == CONNECTION_STATUS_ACTIVE
    assert body["connection"]["displayPhoneNumber"] == "5511999998888"
    assert body["canSend"] is True


@pytest.mark.asyncio
async def test_admin_refresh_connection_returns_renewed_qr_while_connecting(
    api_client, staff_headers, admin_evolution_env, db_session, monkeypatch
):
    db_session.add(
        PlatformWhatsAppConnection(
            status=CONNECTION_STATUS_CONNECTING,
            evolution_instance_name="korus-welcome",
            encrypted_instance_api_key=encrypt_secret("inst-key"),
        )
    )
    await db_session.commit()
    _install_fake(monkeypatch, _ConnectingEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/refresh-connection", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["status"] == CONNECTION_STATUS_CONNECTING
    assert body["qrcodeBase64"] == "qr-renovado"
    assert body["connectionState"] == "connecting"
    assert body["canSend"] is False


@pytest.mark.asyncio
async def test_platform_connection_webhook_marks_admin_whatsapp_active(
    api_client, staff_headers, admin_evolution_env, db_session
):
    db_session.add(
        PlatformWhatsAppConnection(
            status=CONNECTION_STATUS_CONNECTING,
            evolution_instance_name="korus-welcome-webhook",
            encrypted_instance_api_key=encrypt_secret("inst-key"),
        )
    )
    await db_session.commit()

    webhook_response = await api_client.post(
        "/api/v1/webhooks/evolution/whatsapp",
        json={
            "event": "CONNECTION_UPDATE",
            "instance": "korus-welcome-webhook",
            "data": {
                "state": "open",
                "ownerJid": "5511999998888@s.whatsapp.net",
            },
        },
        headers={"Authorization": "Bearer evo-secret"},
    )
    assert webhook_response.status_code == 200

    status_response = await api_client.get(
        "/api/v1/admin/whatsapp/platform", headers=staff_headers
    )
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["connection"]["status"] == CONNECTION_STATUS_ACTIVE
    assert body["connection"]["displayPhoneNumber"] == "5511999998888"
    assert body["canSend"] is True


@pytest.mark.asyncio
async def test_admin_disconnect(
    api_client, staff_headers, admin_evolution_env, db_session, monkeypatch
):
    db_session.add(
        PlatformWhatsAppConnection(
            status=CONNECTION_STATUS_ACTIVE,
            evolution_instance_name="korus-welcome",
            encrypted_instance_api_key=encrypt_secret("inst-key"),
        )
    )
    await db_session.commit()
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    response = await api_client.post(
        "/api/v1/admin/whatsapp/platform/disconnect", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connection"]["status"] == CONNECTION_STATUS_DISCONNECTED
    assert body["canSend"] is False
    assert fake.deleted_instances == ["korus-welcome"]


@pytest.mark.asyncio
async def test_admin_welcome_message_returns_default(api_client, staff_headers):
    response = await api_client.get(
        "/api/v1/admin/whatsapp/platform/message", headers=staff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] is None
    assert "{{firstName}}" in body["defaultMessage"]


@pytest.mark.asyncio
async def test_admin_welcome_message_update_and_reset(
    api_client, staff_headers, db_session
):
    response = await api_client.put(
        "/api/v1/admin/whatsapp/platform/message",
        headers=staff_headers,
        json={"message": "Olá, {{firstName}}! Mensagem personalizada da plataforma."},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Olá, {{firstName}}! Mensagem personalizada da plataforma."

    response = await api_client.get(
        "/api/v1/admin/whatsapp/platform/message", headers=staff_headers
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Olá, {{firstName}}! Mensagem personalizada da plataforma."

    response = await api_client.put(
        "/api/v1/admin/whatsapp/platform/message",
        headers=staff_headers,
        json={"message": None},
    )
    assert response.status_code == 200
    assert response.json()["message"] is None


@pytest.mark.asyncio
async def test_admin_welcome_message_rejects_too_long(api_client, staff_headers):
    response = await api_client.put(
        "/api/v1/admin/whatsapp/platform/message",
        headers=staff_headers,
        json={"message": "a" * 4001},
    )
    assert response.status_code == 422
