from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.refresh_session import RefreshSession
from app.services.refresh_token_service import (
    RefreshSessionInvalidated,
    create_refresh_session,
    revoke_refresh_session,
    rotate_refresh_session,
)


async def test_duplicate_refresh_returns_same_successor(db_session, professional):
    old = await create_refresh_session(db_session, professional)
    _, fresh = await rotate_refresh_session(db_session, old)
    _, repeated = await rotate_refresh_session(db_session, old)
    assert fresh == repeated and fresh != old
    assert professional.token_version == 0
    rows = (await db_session.scalars(select(RefreshSession))).all()
    assert len(rows) == 2
    assert sum(row.revoked_at is None for row in rows) == 1


async def test_old_replay_cannot_keep_revoking_new_logins(db_session, professional):
    old = await create_refresh_session(db_session, professional)
    await rotate_refresh_session(db_session, old)
    original = (await db_session.scalars(select(RefreshSession).where(
        RefreshSession.revoked_at.is_not(None)
    ))).one()
    original.revoked_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    with pytest.raises(RefreshSessionInvalidated):
        await rotate_refresh_session(db_session, old)
    assert professional.token_version == 1
    new_login = await create_refresh_session(db_session, professional)
    with pytest.raises(HTTPException) as exc:
        await rotate_refresh_session(db_session, old)
    assert exc.value.status_code == 401
    assert professional.token_version == 1
    assert (await rotate_refresh_session(db_session, new_login))[0].id == professional.id


async def test_logout_with_previous_cookie_revokes_successor(db_session, professional):
    other_login = await create_refresh_session(db_session, professional)
    old = await create_refresh_session(db_session, professional)
    _, fresh = await rotate_refresh_session(db_session, old)
    await revoke_refresh_session(db_session, old)
    active = (await db_session.scalars(select(RefreshSession).where(
        RefreshSession.revoked_at.is_(None)
    ))).all()
    assert len(active) == 1
    assert (await rotate_refresh_session(db_session, other_login))[0].id == professional.id
    for token in (old, fresh):
        with pytest.raises(HTTPException) as exc:
            await rotate_refresh_session(db_session, token)
        assert exc.value.status_code == 401


async def test_second_to_last_token_is_not_accepted_in_overlap(db_session, professional):
    old = await create_refresh_session(db_session, professional)
    _, middle = await rotate_refresh_session(db_session, old)
    await rotate_refresh_session(db_session, middle)
    with pytest.raises(RefreshSessionInvalidated):
        await rotate_refresh_session(db_session, old)
    assert professional.token_version == 1


@pytest.mark.parametrize("invalid", ["disabled", "expired", "version"])
async def test_overlap_never_restores_invalid_session(db_session, professional, invalid):
    old = await create_refresh_session(db_session, professional)
    await rotate_refresh_session(db_session, old)
    if invalid == "disabled":
        professional.is_disabled = True
    elif invalid == "version":
        professional.token_version += 1
    else:
        for row in (await db_session.scalars(select(RefreshSession))).all():
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await rotate_refresh_session(db_session, old)
    assert exc.value.status_code == 401
