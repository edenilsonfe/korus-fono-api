"""Admin trial email campaigns: eligibility, authorization and delivery tracking."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.professional import Professional
from app.models.trial_email_campaign import TrialEmailCampaign, TrialEmailDelivery
from app.services.trial_email_campaign_service import TrialEmailCampaignService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Professional.__table__,
        AdminAuditLog.__table__,
        TrialEmailCampaign.__table__,
        TrialEmailDelivery.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _professional(
    db: AsyncSession,
    email: str,
    *,
    is_staff: bool = False,
    is_disabled: bool = False,
    email_verified: bool = True,
    subscription_status: str = "trialing",
    trial_ends_at: datetime | None = None,
) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("secret123"),
        name=f"Pessoa {email}",
        specialty="Fonoaudiologia",
        specialty_key="fono",
        is_staff=is_staff,
        is_disabled=is_disabled,
        email_verified_at=datetime.now(UTC) if email_verified else None,
        subscription_status=subscription_status,
        trial_ends_at=trial_ends_at,
    )
    db.add(professional)
    await db.commit()
    await db.refresh(professional)
    return professional


def _headers(professional: Professional) -> dict[str, str]:
    token = create_access_token(professional.id, professional.token_version)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


async def test_trial_email_preview_requires_staff(client, db):
    user = await _professional(
        db,
        "user@example.com",
        trial_ends_at=datetime.now(UTC) + timedelta(days=2),
    )

    response = await client.get(
        "/api/v1/admin/trial-emails/preview?audience=expiring_soon&expiresWithinDays=3",
        headers=_headers(user),
    )

    assert response.status_code == 403


async def test_expired_preview_uses_trial_date_and_excludes_unsafe_recipients(client, db):
    now = datetime.now(UTC)
    staff = await _professional(
        db, "staff@example.com", is_staff=True, trial_ends_at=now + timedelta(days=30)
    )
    await _professional(
        db,
        "expired-stale@example.com",
        subscription_status="trialing",
        trial_ends_at=now - timedelta(hours=1),
    )
    await _professional(
        db,
        "expired-status@example.com",
        subscription_status="trial_expired",
        trial_ends_at=now - timedelta(days=2),
    )
    await _professional(
        db,
        "active@example.com",
        subscription_status="active",
        trial_ends_at=now - timedelta(days=2),
    )
    await _professional(
        db,
        "disabled@example.com",
        is_disabled=True,
        subscription_status="trial_expired",
        trial_ends_at=now - timedelta(days=2),
    )
    await _professional(
        db,
        "unverified@example.com",
        email_verified=False,
        subscription_status="trial_expired",
        trial_ends_at=now - timedelta(days=2),
    )

    response = await client.get(
        "/api/v1/admin/trial-emails/preview?audience=expired",
        headers=_headers(staff),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["eligibleCount"] == 2
    assert body["suppressedCount"] == 0
    assert {item["email"] for item in body["sample"]} == {
        "expired-stale@example.com",
        "expired-status@example.com",
    }
    assert body["subject"]


async def test_expiring_preview_honors_window_and_recent_send_cooldown(client, db):
    now = datetime.now(UTC)
    staff = await _professional(
        db, "staff2@example.com", is_staff=True, trial_ends_at=now + timedelta(days=30)
    )
    inside = await _professional(
        db, "inside@example.com", trial_ends_at=now + timedelta(days=2)
    )
    await _professional(db, "outside@example.com", trial_ends_at=now + timedelta(days=4))

    previous = TrialEmailCampaign(
        actor_id=staff.id,
        audience="expiring_soon",
        expires_within_days=3,
        status="completed",
        eligible_count=1,
        sent_count=1,
        completed_at=now,
    )
    db.add(previous)
    await db.flush()
    db.add(
        TrialEmailDelivery(
            campaign_id=previous.id,
            professional_id=inside.id,
            email=inside.email,
            status="sent",
            sent_at=now,
        )
    )
    await db.commit()

    response = await client.get(
        "/api/v1/admin/trial-emails/preview?audience=expiring_soon&expiresWithinDays=3",
        headers=_headers(staff),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["eligibleCount"] == 0
    assert body["suppressedCount"] == 1
    assert body["sample"] == []


async def test_create_campaign_snapshots_recipients_and_audits(client, db, monkeypatch):
    now = datetime.now(UTC)
    staff = await _professional(
        db, "staff3@example.com", is_staff=True, trial_ends_at=now + timedelta(days=30)
    )
    recipient = await _professional(
        db, "recipient@example.com", trial_ends_at=now + timedelta(days=2)
    )
    enqueue = AsyncMock()
    monkeypatch.setattr("app.api.v1.admin_trial_emails.enqueue_trial_email_campaign", enqueue)

    response = await client.post(
        "/api/v1/admin/trial-emails/campaigns",
        headers=_headers(staff),
        json={"audience": "expiring_soon", "expiresWithinDays": 3},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["eligibleCount"] == 1
    deliveries = (
        await db.execute(
            select(TrialEmailDelivery).where(
                TrialEmailDelivery.campaign_id == UUID(body["id"])
            )
        )
    ).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].professional_id == recipient.id
    audit = (
        await db.execute(
            select(AdminAuditLog).where(
                AdminAuditLog.action == "create_trial_email_campaign"
            )
        )
    ).scalar_one()
    assert audit.actor_id == staff.id
    assert audit.payload["eligible_count"] == 1
    enqueue.assert_awaited_once()

    history = await client.get(
        "/api/v1/admin/trial-emails/campaigns", headers=_headers(staff)
    )
    assert history.status_code == 200
    assert history.json()[0]["id"] == body["id"]


async def test_create_campaign_rejects_invalid_window(client, db):
    staff = await _professional(
        db,
        "staff4@example.com",
        is_staff=True,
        trial_ends_at=datetime.now(UTC) + timedelta(days=30),
    )

    response = await client.post(
        "/api/v1/admin/trial-emails/campaigns",
        headers=_headers(staff),
        json={"audience": "expiring_soon", "expiresWithinDays": 31},
    )

    assert response.status_code == 422


async def test_process_campaign_tracks_provider_success_and_failure(client, db, monkeypatch):
    now = datetime.now(UTC)
    staff = await _professional(
        db, "staff5@example.com", is_staff=True, trial_ends_at=now + timedelta(days=30)
    )
    await _professional(db, "success@example.com", trial_ends_at=now + timedelta(days=1))
    await _professional(db, "failure@example.com", trial_ends_at=now + timedelta(days=1))
    monkeypatch.setattr(
        "app.api.v1.admin_trial_emails.enqueue_trial_email_campaign", AsyncMock()
    )

    response = await client.post(
        "/api/v1/admin/trial-emails/campaigns",
        headers=_headers(staff),
        json={"audience": "expiring_soon", "expiresWithinDays": 3},
    )
    campaign_id = response.json()["id"]

    def fake_send(to_email: str, **_kwargs):
        if to_email == "failure@example.com":
            raise RuntimeError("provider unavailable")
        return "resend-message-id"

    monkeypatch.setattr(
        "app.services.trial_email_campaign_service.send_email", fake_send
    )
    campaign = await TrialEmailCampaignService(db).process_campaign(campaign_id)

    assert campaign.status == "completed"
    assert campaign.sent_count == 1
    assert campaign.failed_count == 1
    deliveries = (
        await db.execute(
            select(TrialEmailDelivery).where(
                TrialEmailDelivery.campaign_id == campaign.id
            )
        )
    ).scalars().all()
    assert {delivery.status for delivery in deliveries} == {"sent", "failed"}
    sent = next(delivery for delivery in deliveries if delivery.status == "sent")
    assert sent.provider_message_id == "resend-message-id"
