"""Tests for the support contact channel (service + HTTP flow)."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine as _real_engine
from app.services.support_contact import send_support_contact_sync


@pytest.fixture(autouse=True)
async def _patch_middleware_db(monkeypatch):
    """EntitlementMiddleware usa AsyncSessionLocal (engine Postgres real) direto,
    e o pytest-asyncio troca o event loop a cada teste — conexões asyncpg
    reaproveitadas entre loops quebram. Aponta o middleware para um sqlite em
    memória vazio (novo por teste), mantendo a semântica: profissional ausente
    -> middleware deixa a requisição passar."""
    # Solta conexões asyncpg órfãs de outros arquivos de teste (loop fechado)
    # sem tentar fechá-las — evita GC tardio estourando no loop deste teste.
    await _real_engine.dispose(close=False)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", maker)
    yield


def test_send_support_contact_sync_sends_when_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "support_contact_email", "suporte@korusfono.com.br")

    send_mock = MagicMock(return_value="msg-id")
    monkeypatch.setattr("app.services.support_contact.send_email", send_mock)

    send_support_contact_sync(
        professional_name="Dra. Teste",
        professional_email="dra@teste.com",
        subject="Dúvida",
        message="Preciso de ajuda com a plataforma.",
    )

    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "suporte@korusfono.com.br"
    assert "[Suporte KorusFono]" in kwargs["subject"]
    assert "Dra. Teste" in kwargs["html"]
    assert "dra@teste.com" in kwargs["html"]
    assert "Preciso de ajuda" in kwargs["html"]


def test_send_support_contact_sync_skips_without_recipient(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "support_contact_email", "")

    send_mock = MagicMock()
    monkeypatch.setattr("app.services.support_contact.send_email", send_mock)

    send_support_contact_sync(
        professional_name="Dra. Teste",
        professional_email="dra@teste.com",
        subject="Dúvida",
        message="Preciso de ajuda com a plataforma.",
    )

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_http_contact_requires_auth(api_client):
    resp = await api_client.post(
        "/api/v1/support/contact",
        json={"subject": "Dúvida", "message": "Preciso de ajuda com a plataforma."},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_http_contact_503_when_not_configured(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/support/contact",
        headers=auth_headers,
        json={"subject": "Dúvida", "message": "Preciso de ajuda com a plataforma."},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_http_contact_sends(api_client, auth_headers, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "support_contact_email", "suporte@korusfono.com.br")

    send_mock = MagicMock(return_value="msg-id")
    monkeypatch.setattr("app.services.support_contact.send_email", send_mock)

    resp = await api_client.post(
        "/api/v1/support/contact",
        headers=auth_headers,
        json={"subject": "Dúvida", "message": "Preciso de ajuda com a plataforma."},
    )
    assert resp.status_code == 200
    assert resp.json()["message"]

    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "suporte@korusfono.com.br"
    assert "Dra. Teste" in kwargs["html"]
    assert "protocol-test@example.com" in kwargs["html"]


@pytest.mark.asyncio
async def test_http_contact_validates_subject(api_client, auth_headers, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "support_contact_email", "suporte@korusfono.com.br")

    resp = await api_client.post(
        "/api/v1/support/contact",
        headers=auth_headers,
        json={"subject": "ab", "message": "Preciso de ajuda com a plataforma."},
    )
    assert resp.status_code == 422
