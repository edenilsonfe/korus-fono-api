"""Tests for the WhatsApp welcome message sent after registration."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.whatsapp_connection import CONNECTION_STATUS_ACTIVE, CONNECTION_STATUS_NOT_CONNECTED
from app.services.evolution_api_client import EvolutionApiClient, EvolutionApiError
from app.services.whatsapp_welcome_service import (
    WELCOME_MESSAGE,
    _render_welcome_message,
    send_whatsapp_welcome_message,
)
from app.utils import credential_encryption as cred
from app.utils.credential_encryption import encrypt_secret


class _FakeEvolutionClient:
    """Drop-in replacement for EvolutionApiClient used by the service."""

    extract_qrcode_base64 = staticmethod(EvolutionApiClient.extract_qrcode_base64)
    extract_instance_api_key = staticmethod(EvolutionApiClient.extract_instance_api_key)
    extract_connection_state = staticmethod(EvolutionApiClient.extract_connection_state)

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.sent: list[tuple[str, str, str]] = []

    async def connection_state(self, instance_name, *, api_key):
        return {"instance": {"state": "open"}}

    async def check_whatsapp_numbers(self, instance_name, numbers, *, api_key):
        return [
            {"number": n, "exists": True, "jid": f"{n}@s.whatsapp.net"}
            for n in numbers
        ]

    async def send_text(self, instance_name, number, text, *, api_key):
        self.sent.append((instance_name, number, text))
        return {"key": {"id": "welcome-msg-1"}}


class _CheckFailsClient(_FakeEvolutionClient):
    async def check_whatsapp_numbers(self, instance_name, numbers, *, api_key):
        raise EvolutionApiError("boom")


class _NoWhatsAppClient(_FakeEvolutionClient):
    async def check_whatsapp_numbers(self, instance_name, numbers, *, api_key):
        return [{"number": n, "exists": False} for n in numbers]


class _ClosedEvolutionClient(_FakeEvolutionClient):
    async def connection_state(self, instance_name, *, api_key):
        return {"instance": {"state": "close"}}


@pytest.fixture
def welcome_env(monkeypatch):
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
def welcome_session_factory(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.whatsapp_welcome_service.AsyncSessionLocal", factory
    )
    return factory


async def _active_platform_connection(db_session) -> PlatformWhatsAppConnection:
    connection = PlatformWhatsAppConnection(
        status=CONNECTION_STATUS_ACTIVE,
        evolution_instance_name="korus-welcome",
        encrypted_instance_api_key=encrypt_secret("inst-key"),
    )
    db_session.add(connection)
    await db_session.commit()
    await db_session.refresh(connection)
    return connection


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr(
        "app.services.platform_whatsapp_service.EvolutionApiClient",
        lambda: fake,
    )
    return fake


def test_welcome_message_copy_mentions_user_and_help():
    rendered = _render_welcome_message(WELCOME_MESSAGE, "Ana")
    assert "{{firstName}}" in WELCOME_MESSAGE
    assert "Ana" in rendered
    assert "Korus Fono" in rendered
    assert "bem-vindo(a)" in rendered
    assert "dúvida" in rendered
    assert "ajuda" in rendered


@pytest.mark.asyncio
async def test_send_welcome_sends_from_platform_connection(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    await _active_platform_connection(db_session)
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(
        user_name="Ana Souza",
        phone="(11) 98888-7777",
    )

    assert sent is True
    assert fake.sent
    instance_name, number, text = fake.sent[0]
    assert instance_name == "korus-welcome"
    assert number == "5511988887777"
    assert "Ana" in text


@pytest.mark.asyncio
async def test_send_welcome_uses_custom_message_when_stored(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    connection = await _active_platform_connection(db_session)
    connection.welcome_message = "Bem-vindo(a), {{firstName}}! Conta com a gente. 💛"
    await db_session.commit()
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(
        user_name="Ana Souza",
        phone="(11) 98888-7777",
    )

    assert sent is True
    assert fake.sent
    assert fake.sent[0][2] == "Bem-vindo(a), Ana! Conta com a gente. 💛"


@pytest.mark.asyncio
async def test_send_welcome_skips_when_no_connection(
    welcome_env, welcome_session_factory, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_skips_when_connection_not_active(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    db_session.add(
        PlatformWhatsAppConnection(status=CONNECTION_STATUS_NOT_CONNECTED)
    )
    await db_session.commit()
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_reconciles_stale_active_connection_before_sending(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    connection = await _active_platform_connection(db_session)
    fake = _install_fake(monkeypatch, _ClosedEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    await db_session.refresh(connection)
    assert sent is False
    assert fake.sent == []
    assert connection.status == "needs_reconnect"


@pytest.mark.asyncio
async def test_send_welcome_skips_for_non_evolution_provider(
    welcome_env, welcome_session_factory, monkeypatch
):
    welcome_env.whatsapp_provider = "meta"
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_skips_without_phone(
    welcome_env, welcome_session_factory, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_skips_invalid_phone(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    await _active_platform_connection(db_session)
    fake = _install_fake(monkeypatch, _FakeEvolutionClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="123")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_skips_number_without_whatsapp(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    await _active_platform_connection(db_session)
    fake = _install_fake(monkeypatch, _NoWhatsAppClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    assert sent is False
    assert fake.sent == []


@pytest.mark.asyncio
async def test_send_welcome_falls_back_when_number_check_fails(
    welcome_env, welcome_session_factory, db_session, monkeypatch
):
    await _active_platform_connection(db_session)
    fake = _install_fake(monkeypatch, _CheckFailsClient())

    sent = await send_whatsapp_welcome_message(user_name="Ana", phone="11988887777")

    assert sent is True
    assert fake.sent
    assert fake.sent[0][1] == "5511988887777"


@pytest.mark.asyncio
async def test_http_register_queues_whatsapp_welcome_task(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None
    )

    task_mock = MagicMock()
    monkeypatch.setattr("app.api.v1.auth.send_whatsapp_welcome_task", task_mock)

    email = f"welcome-{uuid4().hex[:8]}@test.com"
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "securepass123",
            "name": "Welcome User",
            "specialtyKey": "fono",
            "council": "CRFa 999",
            "phone": "(11) 97777-6666",
        },
    )
    assert reg.status_code == 201
    assert task_mock.called
    args = task_mock.call_args.args
    assert args[0] == "Welcome User"
    assert args[1] == "(11) 97777-6666"


@pytest.mark.asyncio
async def test_http_register_rejects_missing_phone_for_whatsapp_welcome(
    api_client, monkeypatch
):
    monkeypatch.setattr(
        "app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None
    )

    reg = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"welcome-no-phone-{uuid4().hex[:8]}@test.com",
            "password": "securepass123",
            "name": "Welcome User",
            "specialtyKey": "fono",
            "council": "CRFa 999",
        },
    )

    assert reg.status_code == 422


@pytest.mark.asyncio
async def test_http_register_rejects_invalid_whatsapp_phone(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None
    )

    reg = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"welcome-invalid-phone-{uuid4().hex[:8]}@test.com",
            "password": "securepass123",
            "name": "Welcome User",
            "specialtyKey": "fono",
            "council": "CRFa 999",
            "phone": "123",
        },
    )

    assert reg.status_code == 422
