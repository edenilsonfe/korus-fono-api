"""Tests for the in-app notification inbox (broadcast, no fan-out).

Standalone setup: creates only the tables needed (professionals,
app_notifications, app_notification_reads) on an in-memory SQLite DB, so it
does not depend on the project-wide conftest fixtures (which create all tables
and would trip on PostgreSQL-only JSONB columns on SQLite).

The service is cross-dialect (audience matching and read upserts are done in
Python/manual), so these tests validate the same logic that runs on PostgreSQL
in production.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.app_notification import AppNotification, AppNotificationRead
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.app_notification import AnnouncementCreate, AnnouncementUpdate
from app.services.notification_service import (
    InvalidStatusTransitionError,
    NotificationNotVisibleError,
    NotificationService,
)

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        # Create only the tables we need (no JSONB columns among these).
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    AppNotification.__table__,
                    AppNotificationRead.__table__,
                    NotificationSettings.__table__,
                    Patient.__table__,
                    AdminAuditLog.__table__,
                ],
            )
        )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _make_professional(db, *, email, specialty_key="fono", is_staff=False):
    pro = Professional(
        email=email,
        password_hash="x",
        name=f"Pro {email}",
        specialty_key=specialty_key,
        specialty=specialty_key.title(),
        council="CRFa",
        phone="11999990000",
        is_staff=is_staff,
        email_verified_at=datetime.now(timezone.utc),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


async def _make_announcement(
    db,
    *,
    type="feature",
    title="Anúncio",
    body="Corpo",
    severity="info",
    audience="all",
    status="published",
    deep_link=None,
    publish_at=None,
    expires_at=None,
    author_id=None,
):
    ann = AppNotification(
        kind="broadcast",
        type=type,
        title=title,
        body=body,
        severity=severity,
        audience=audience,
        status=status,
        deep_link=deep_link,
        publish_at=publish_at,
        expires_at=expires_at,
        created_by=author_id,
    )
    db.add(ann)
    await db.commit()
    await db.refresh(ann)
    return ann


def _auth_headers(professional):
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


async def _client(db):
    factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear_override():
    app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# Service-level tests
# --------------------------------------------------------------------------- #


async def test_broadcast_without_reads_counts_as_unseen_unread(db):
    pro = await _make_professional(db, email="p1@example.com")
    await _make_announcement(db, title="A", audience="all")
    service = NotificationService(db)
    counts = await service.counts_for_professional(pro)
    assert counts.badge == 1
    assert counts.unread == 1


async def test_mark_seen_zeros_badge_but_keeps_unread(db):
    pro = await _make_professional(db, email="p2@example.com")
    await _make_announcement(db, title="A", audience="all")
    service = NotificationService(db)
    counts = await service.mark_seen(pro)
    assert counts.badge == 0
    assert counts.unread == 1


async def test_mark_read_zeros_unread_and_seen(db):
    pro = await _make_professional(db, email="p3@example.com")
    ann = await _make_announcement(db, title="A", audience="all")
    service = NotificationService(db)
    item = await service.mark_read(pro, ann.id)
    assert item.seen is True
    assert item.read is True
    counts = await service.counts_for_professional(pro)
    assert counts.badge == 0
    assert counts.unread == 0


async def test_seen_neq_read(db):
    pro = await _make_professional(db, email="p4@example.com")
    await _make_announcement(db, title="A", audience="all")
    await _make_announcement(db, title="B", audience="all")
    service = NotificationService(db)
    await service.mark_seen(pro)
    counts = await service.counts_for_professional(pro)
    assert counts.badge == 0
    assert counts.unread == 2


async def test_audience_all_reaches_all_specialties(db):
    pro_fono = await _make_professional(db, email="fono@x.com", specialty_key="fono")
    pro_to = await _make_professional(db, email="to@x.com", specialty_key="to")
    await _make_announcement(db, title="A", audience="all")
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro_fono)).badge == 1
    assert (await service.counts_for_professional(pro_to)).badge == 1


async def test_audience_specialty_matches_only_listed(db):
    pro_fono = await _make_professional(db, email="fono2@x.com", specialty_key="fono")
    pro_to = await _make_professional(db, email="to2@x.com", specialty_key="to")
    pro_psi = await _make_professional(db, email="psi@x.com", specialty_key="psicologia")
    await _make_announcement(db, title="A", audience="fono,to")
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro_fono)).badge == 1
    assert (await service.counts_for_professional(pro_to)).badge == 1
    assert (await service.counts_for_professional(pro_psi)).badge == 0


async def test_scheduled_with_future_publish_at_not_vigent(db):
    pro = await _make_professional(db, email="sch@x.com")
    future = datetime.now(timezone.utc) + timedelta(days=2)
    await _make_announcement(db, title="A", status="scheduled", publish_at=future)
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro)).badge == 0


async def test_scheduled_with_past_publish_at_is_vigent(db):
    pro = await _make_professional(db, email="sch2@x.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await _make_announcement(db, title="A", status="scheduled", publish_at=past)
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro)).badge == 1


async def test_expired_not_vigent(db):
    pro = await _make_professional(db, email="exp@x.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await _make_announcement(db, title="A", status="published", expires_at=past)
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro)).badge == 0


async def test_archived_not_vigent(db):
    pro = await _make_professional(db, email="arch@x.com")
    await _make_announcement(db, title="A", status="archived")
    service = NotificationService(db)
    assert (await service.counts_for_professional(pro)).badge == 0


async def test_read_invisible_notification_raises(db):
    pro = await _make_professional(db, email="inv@x.com", specialty_key="fono")
    ann = await _make_announcement(db, title="A", audience="to")
    service = NotificationService(db)
    with pytest.raises(NotificationNotVisibleError):
        await service.mark_read(pro, ann.id)


async def test_status_transition_draft_to_published(db):
    pro = await _make_professional(db, email="staff@x.com", is_staff=True)
    service = NotificationService(db)
    ann = await service.create_announcement(
        author=pro, payload=AnnouncementCreate(type="feature", title="A", body="B")
    )
    assert ann.status == "draft"
    updated = await service.update_announcement(
        notification_id=ann.id, payload=AnnouncementUpdate(status="published")
    )
    assert updated.status == "published"


async def test_status_transition_scheduled_requires_future_publish_at(db):
    pro = await _make_professional(db, email="staff2@x.com", is_staff=True)
    service = NotificationService(db)
    ann = await service.create_announcement(
        author=pro, payload=AnnouncementCreate(type="feature", title="A", body="B")
    )
    with pytest.raises(InvalidStatusTransitionError):
        await service.update_announcement(
            notification_id=ann.id, payload=AnnouncementUpdate(status="scheduled")
        )


async def test_invalid_status_transition_rejected(db):
    pro = await _make_professional(db, email="staff3@x.com", is_staff=True)
    service = NotificationService(db)
    ann = await service.create_announcement(
        author=pro, payload=AnnouncementCreate(type="feature", title="A", body="B")
    )
    # draft -> archived is invalid
    with pytest.raises(InvalidStatusTransitionError):
        await service.update_announcement(
            notification_id=ann.id, payload=AnnouncementUpdate(status="archived")
        )


async def test_announcement_stats(db):
    pro_fono = await _make_professional(db, email="s1@x.com", specialty_key="fono")
    pro_to = await _make_professional(db, email="s2@x.com", specialty_key="to")
    await _make_professional(db, email="s3@x.com", specialty_key="psicologia")
    ann = await _make_announcement(db, title="A", audience="fono,to")
    service = NotificationService(db)
    await service.mark_read(pro_fono, ann.id)
    await service.mark_seen(pro_to)
    stats = await service.announcement_stats(ann.id)
    assert stats.audience_size == 2  # fono + to
    assert stats.seen_count == 2
    assert stats.read_count == 1
    assert stats.click_count == 1
    assert stats.seen_rate == 1.0
    assert stats.read_rate == 0.5


async def test_cursor_pagination(db):
    pro = await _make_professional(db, email="page@x.com")
    anns = []
    for i in range(3):
        ann = await _make_announcement(db, title=f"A{i}")
        anns.append(ann)
    service = NotificationService(db)
    page1 = await service.list_for_professional(professional=pro, limit=2)
    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    page2 = await service.list_for_professional(
        professional=pro, limit=2, cursor=page1.next_cursor
    )
    assert len(page2.items) == 1
    assert page2.next_cursor is None
    ids1 = {i.id for i in page1.items}
    ids2 = {i.id for i in page2.items}
    assert ids1.isdisjoint(ids2)
    assert ids1 | ids2 == {str(a.id) for a in anns}


async def test_mark_seen_idempotent(db):
    pro = await _make_professional(db, email="idem@x.com")
    await _make_announcement(db, title="A", audience="all")
    service = NotificationService(db)
    await service.mark_seen(pro)
    counts = await service.mark_seen(pro)  # second call should not error
    assert counts.badge == 0


# --------------------------------------------------------------------------- #
# API / HTTP tests
# --------------------------------------------------------------------------- #


async def test_http_unread_count(db):
    pro = await _make_professional(db, email="h1@x.com")
    await _make_announcement(db, title="A", audience="all")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/notifications/unread-count", headers=_auth_headers(pro))
    assert resp.status_code == 200
    body = resp.json()
    assert body["badge"] == 1
    assert body["unread"] == 1
    _clear_override()


async def test_http_list_notifications(db):
    pro = await _make_professional(db, email="h2@x.com")
    await _make_announcement(db, title="A", audience="all")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/notifications", headers=_auth_headers(pro))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "A"
    assert body["items"][0]["seen"] is False
    _clear_override()


async def test_http_mark_seen_zeros_badge(db):
    pro = await _make_professional(db, email="h3@x.com")
    await _make_announcement(db, title="A", audience="all")
    client = await _client(db)
    async with client:
        resp = await client.post("/api/v1/notifications/seen", headers=_auth_headers(pro))
    assert resp.status_code == 200
    body = resp.json()
    assert body["badge"] == 0
    assert body["unread"] == 1
    _clear_override()


async def test_http_mark_read(db):
    pro = await _make_professional(db, email="h4@x.com")
    ann = await _make_announcement(db, title="A", audience="all")
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/notifications/{ann.id}/read", headers=_auth_headers(pro)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["read"] is True
    assert body["seen"] is True
    _clear_override()


async def test_http_mark_read_invisible_returns_404(db):
    pro = await _make_professional(db, email="h5@x.com", specialty_key="fono")
    ann = await _make_announcement(db, title="A", audience="to")
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/notifications/{ann.id}/read", headers=_auth_headers(pro)
        )
    assert resp.status_code == 404
    _clear_override()


async def test_http_read_all(db):
    pro = await _make_professional(db, email="h6@x.com")
    await _make_announcement(db, title="A", audience="all")
    await _make_announcement(db, title="B", audience="all")
    client = await _client(db)
    async with client:
        resp = await client.post("/api/v1/notifications/read-all", headers=_auth_headers(pro))
    assert resp.status_code == 200
    body = resp.json()
    assert body["badge"] == 0
    assert body["unread"] == 0
    _clear_override()


async def test_http_admin_announcements_gate_is_staff(db):
    non_staff = await _make_professional(db, email="nonstaff@x.com", is_staff=False)
    staff = await _make_professional(db, email="staffapi@x.com", is_staff=True)
    client = await _client(db)
    async with client:
        resp = await client.post(
            "/api/v1/announcements",
            headers=_auth_headers(non_staff),
            json={"type": "feature", "title": "A", "body": "B"},
        )
        assert resp.status_code == 403
        resp = await client.post(
            "/api/v1/announcements",
            headers=_auth_headers(staff),
            json={"type": "feature", "title": "A", "body": "B"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "draft"
    _clear_override()


async def test_http_admin_list_and_stats(db):
    staff = await _make_professional(db, email="stafflist@x.com", is_staff=True)
    pro = await _make_professional(db, email="listprof@x.com", specialty_key="fono")
    ann = await _make_announcement(db, title="A", audience="all", author_id=staff.id)
    service = NotificationService(db)
    await service.mark_read(pro, ann.id)
    await db.commit()
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/announcements", headers=_auth_headers(staff))
        assert resp.status_code == 200
        items = resp.json()
        assert any(i["id"] == str(ann.id) for i in items)
        resp = await client.get(
            f"/api/v1/announcements/{ann.id}/stats", headers=_auth_headers(staff)
        )
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["audienceSize"] >= 1
        assert stats["readCount"] == 1
    _clear_override()


async def test_http_me_returns_is_staff(db):
    staff = await _make_professional(db, email="mestaff@x.com", is_staff=True)
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/me", headers=_auth_headers(staff))
    assert resp.status_code == 200
    body = resp.json()
    assert body["isStaff"] is True
    _clear_override()
