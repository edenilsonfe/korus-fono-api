"""Service-level tests for email verification (no HTTP wiring)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles

from app.models.password_reset_token import PURPOSE_EMAIL_VERIFICATION, PasswordResetToken
from app.services.email.templates import email_verification_email
from app.services.email_verification import (
    request_email_verification,
    send_email_verification_email_sync,
    verify_email_with_token,
)
from app.utils.token_hash import hash_token


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value


def test_email_verification_email_template_copy():
    rendered = email_verification_email(
        user_name="Ana",
        verify_url="https://app.example.com/verificar-email?token=abc",
        expires_minutes=1440,
    )

    assert "Confirme seu e-mail" in rendered.subject or "Confirme seu e-mail" in rendered.html
    assert "Olá Ana," in rendered.html
    assert 'href="https://app.example.com/verificar-email?token=abc"' in rendered.html
    assert "https://app.example.com/verificar-email?token=abc" in rendered.text
    assert "1440" in rendered.html or "1440" in rendered.text


@pytest.mark.asyncio
async def test_request_email_verification_creates_token(db_session, professional):
    assert professional.email_verified_at is None

    raw_token = await request_email_verification(db_session, professional)

    assert raw_token
    token_result = await db_session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.professional_id == professional.id,
            PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
        )
    )
    token = token_result.scalar_one()
    assert token.token_hash == hash_token(raw_token)
    assert token.used_at is None
    assert token.purpose == PURPOSE_EMAIL_VERIFICATION


@pytest.mark.asyncio
async def test_request_email_verification_cooldown_blocks_second(db_session, professional):
    fake_redis = _FakeRedis()

    first = await request_email_verification(
        db_session, professional, redis_client=fake_redis
    )
    second = await request_email_verification(
        db_session, professional, redis_client=fake_redis
    )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_request_email_verification_force_bypasses_cooldown(db_session, professional):
    fake_redis = _FakeRedis()

    first = await request_email_verification(
        db_session, professional, redis_client=fake_redis
    )
    forced = await request_email_verification(
        db_session, professional, redis_client=fake_redis, force=True
    )

    assert first is not None
    assert forced is not None
    assert forced != first


@pytest.mark.asyncio
async def test_verify_email_with_token_success(db_session, professional):
    raw_token = await request_email_verification(db_session, professional)
    assert raw_token

    updated = await verify_email_with_token(db_session, raw_token)
    await db_session.refresh(professional)

    assert updated.id == professional.id
    assert professional.email_verified_at is not None

    token_result = await db_session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(raw_token),
            PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
        )
    )
    token = token_result.scalar_one()
    assert token.used_at is not None


@pytest.mark.asyncio
async def test_verify_email_with_invalid_token_raises_400(db_session):
    with pytest.raises(HTTPException) as exc:
        await verify_email_with_token(db_session, "token-invalido")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Token inválido ou expirado"


@pytest.mark.asyncio
async def test_verify_email_idempotent_already_verified(db_session, professional):
    raw_token = await request_email_verification(db_session, professional)
    assert raw_token

    await verify_email_with_token(db_session, raw_token)
    await db_session.refresh(professional)
    verified_at = professional.email_verified_at
    assert verified_at is not None

    # Same (now used) token must still succeed when already verified
    again = await verify_email_with_token(db_session, raw_token)
    await db_session.refresh(professional)

    assert again.id == professional.id
    assert professional.email_verified_at == verified_at


@pytest.mark.asyncio
async def test_verify_email_expired_token_raises_400_when_unverified(db_session, professional):
    raw_token = await request_email_verification(db_session, professional)
    assert raw_token

    token_result = await db_session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(raw_token),
            PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
        )
    )
    token = token_result.scalar_one()
    token.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await verify_email_with_token(db_session, raw_token)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Token inválido ou expirado"
    await db_session.refresh(professional)
    assert professional.email_verified_at is None


def test_send_email_verification_email_sync_never_logs_token_when_disabled(caplog, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "email_sending_enabled", False)
    raw_token = "super-secret-verify-token"

    with caplog.at_level("DEBUG"):
        send_email_verification_email_sync("user@example.com", "Usuário", raw_token)

    log_text = caplog.text
    assert raw_token not in log_text
    assert "token=" not in log_text
