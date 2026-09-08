from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.professional import Professional
from app.models.refresh_session import RefreshSession
from app.utils.token_hash import hash_token


class RefreshSessionInvalidated(HTTPException):
    """The auth boundary must commit revocation before returning this rejection."""

    def __init__(self, detail: str = "Sessão invalidada. Faça login novamente."):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _new_raw_token() -> str:
    return secrets.token_urlsafe(32)


async def create_refresh_session(db: AsyncSession, professional: Professional) -> str:
    now = datetime.now(UTC)
    settings = get_settings()
    raw_token = _new_raw_token()
    family_id = uuid.uuid4()
    db.add(
        RefreshSession(
            professional_id=professional.id,
            token_hash=hash_token(raw_token),
            family_id=family_id,
            token_version=professional.token_version,
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.flush()
    return raw_token


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID, *, now: datetime) -> None:
    await db.execute(
        update(RefreshSession)
        .where(
            RefreshSession.family_id == family_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


async def _invalidate_professional_sessions(db: AsyncSession, professional: Professional) -> None:
    now = datetime.now(UTC)
    professional.token_version += 1
    await db.execute(
        update(RefreshSession)
        .where(
            RefreshSession.professional_id == professional.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


async def rotate_refresh_session(
    db: AsyncSession,
    raw_token: str,
) -> tuple[Professional, str]:
    now = datetime.now(UTC)
    token_hash = hash_token(raw_token)
    professional_id = await db.scalar(
        select(RefreshSession.professional_id).where(RefreshSession.token_hash == token_hash)
    )
    # Lock the owner first: revocation touches every session, so locking tokens
    # first can deadlock two concurrent refreshes from the same professional.
    professional = await db.scalar(
        select(Professional).where(Professional.id == professional_id)
        .with_for_update().execution_options(populate_existing=True)
    ) if professional_id else None
    result = await db.execute(
        select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        .with_for_update().execution_options(populate_existing=True)
    )
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")

    if session.revoked_at is not None:
        if professional is not None:
            await _invalidate_professional_sessions(db, professional)
            await db.flush()
        raise RefreshSessionInvalidated()

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        session.revoked_at = now
        await db.flush()
        raise RefreshSessionInvalidated("Token inválido")

    if professional is None or professional.is_disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Conta desativada")

    if session.token_version != professional.token_version:
        await _invalidate_professional_sessions(db, professional)
        await db.flush()
        raise RefreshSessionInvalidated()

    session.revoked_at = now
    new_raw = _new_raw_token()
    db.add(
        RefreshSession(
            professional_id=professional.id,
            token_hash=hash_token(new_raw),
            family_id=session.family_id,
            token_version=professional.token_version,
            expires_at=now + timedelta(days=get_settings().refresh_token_expire_days),
        )
    )
    await db.flush()
    return professional, new_raw


async def revoke_refresh_session(db: AsyncSession, raw_token: str) -> None:
    now = datetime.now(UTC)
    token_hash = hash_token(raw_token)
    result = await db.execute(select(RefreshSession).where(RefreshSession.token_hash == token_hash))
    session = result.scalar_one_or_none()
    if session is not None and session.revoked_at is None:
        session.revoked_at = now
        await db.flush()


async def revoke_all_refresh_sessions(db: AsyncSession, professional: Professional) -> None:
    await _invalidate_professional_sessions(db, professional)
    await db.flush()
