from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.password_reset_token import PURPOSE_EMAIL_VERIFICATION, PasswordResetToken
from app.models.professional import Professional
from app.services.email.resend_client import send_email
from app.services.email.templates import email_verification_email
from app.utils.token_hash import hash_token

logger = logging.getLogger(__name__)


def _redis_client() -> Any | None:
    try:
        import redis

        return redis.from_url(get_settings().redis_url, decode_responses=True)
    except Exception as exc:  # pragma: no cover - fail-open when Redis unavailable
        logger.warning("Email verification Redis unavailable (fail-open): %s", exc)
        return None


async def _create_email_verification_token(
    db: AsyncSession,
    professional_id: UUID,
) -> str:
    now = datetime.now(UTC)
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)

    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.professional_id == professional_id,
            PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )

    db.add(
        PasswordResetToken(
            professional_id=professional_id,
            token_hash=hash_token(raw_token),
            purpose=PURPOSE_EMAIL_VERIFICATION,
            expires_at=now + timedelta(minutes=settings.email_verification_expire_minutes),
        )
    )
    await db.commit()
    return raw_token


async def request_email_verification(
    db: AsyncSession,
    professional: Professional,
    *,
    redis_client: Any = None,
    force: bool = False,
) -> str | None:
    if professional.email_verified_at is not None:
        return None

    client = redis_client if redis_client is not None else _redis_client()
    cooldown_key = f"email_verify_cooldown:{professional.id}"

    if not force and client is not None:
        try:
            if client.get(cooldown_key):
                return None
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            logger.warning("Email verification cooldown read failed (fail-open): %s", exc)

    raw_token = await _create_email_verification_token(db, professional.id)

    if client is not None:
        settings = get_settings()
        try:
            client.set(cooldown_key, "1", ex=settings.email_verification_cooldown_seconds)
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            logger.warning("Email verification cooldown write failed (fail-open): %s", exc)

    return raw_token


def send_email_verification_email_sync(to_email: str, user_name: str, raw_token: str) -> None:
    settings = get_settings()
    base_url = (settings.frontend_url or "").rstrip("/")
    verify_url = f"{base_url}/verificar-email?token={quote_plus(raw_token)}"

    if not settings.email_sending_enabled:
        logger.info(
            "Email sending disabled; verification token created for professional "
            "(email omitted from logs)"
        )
        return

    rendered = email_verification_email(
        user_name=user_name,
        verify_url=verify_url,
        expires_minutes=settings.email_verification_expire_minutes,
    )
    try:
        send_email(
            to_email=to_email,
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
        )
    except Exception as exc:
        logger.exception("Failed to send email verification to %s: %s", to_email, exc)


async def verify_email_with_token(db: AsyncSession, raw_token: str) -> Professional:
    now = datetime.now(UTC)
    token_hash = hash_token(raw_token)

    # Resolve by hash + purpose without used/expires filters (idempotency)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado",
        )

    professional = await db.get(Professional, token.professional_id)
    if professional is None or professional.is_disabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado",
        )

    if professional.email_verified_at is not None:
        return professional

    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if token.used_at is not None or expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado",
        )

    professional.email_verified_at = now
    token.used_at = now

    await db.commit()
    await db.refresh(professional)
    return professional
