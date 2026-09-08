"""Tests for admin billing metrics, coupons and plans."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.utils import utcnow
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.billing import Plan, Subscription
from app.models.coupon import Coupon, CouponRedemption
from app.models.professional import Professional
from app.services.coupon_service import CouponService

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@compiles(JSONB, "sqlite")
def _jb(_t, _c, **_k):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _ar(_t, _c, **_k):
    return "TEXT"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    tables = [
        Professional.__table__,
        AdminAuditLog.__table__,
        Plan.__table__,
        Subscription.__table__,
        Coupon.__table__,
        CouponRedemption.__table__,
    ]
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(bind=c, tables=tables))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        plan = Plan(
            slug="pro_monthly",
            name="Pro Mensal",
            price_cents=9700,
            billing_interval="monthly",
            is_active=True,
        )
        yearly = Plan(
            slug="pro_yearly",
            name="Pro Anual",
            price_cents=97000,
            billing_interval="yearly",
            is_active=True,
        )
        session.add_all([plan, yearly])
        await session.commit()
        await session.refresh(plan)
        await session.refresh(yearly)
        session.info["plan"] = plan
        session.info["yearly"] = yearly
        yield session


async def _pro(db, email, **kw):
    p = Professional(
        email_verified_at=utcnow(),
        email=email,
        password_hash=hash_password("x"),
        name=email,
        specialty_key="fono",
        specialty="Fono",
        is_staff=kw.get("is_staff", False),
        subscription_status=kw.get("subscription_status", "trialing"),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _auth(p):
    return {"Authorization": f"Bearer {create_access_token(p.id, p.token_version)}"}


async def _client(db):
    async def ov():
        yield db

    app.dependency_overrides[get_db] = ov
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear():
    app.dependency_overrides.clear()


async def test_mrr_only_active(db):
    staff = await _pro(db, "s@x.com", is_staff=True)
    user = await _pro(db, "u@x.com", subscription_status="active")
    plan = db.info["plan"]
    yearly = db.info["yearly"]
    db.add(
        Subscription(
            professional_id=user.id, plan_id=plan.id, status="active", provider="stub"
        )
    )
    other = await _pro(db, "y@x.com", subscription_status="active")
    db.add(
        Subscription(
            professional_id=other.id, plan_id=yearly.id, status="active", provider="stub"
        )
    )
    incomplete = await _pro(db, "i@x.com")
    db.add(
        Subscription(
            professional_id=incomplete.id, plan_id=plan.id, status="incomplete", provider="stub"
        )
    )
    await db.commit()

    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/billing/metrics?periodDays=30", headers=_auth(staff))
        assert resp.status_code == 200
        # 9700 monthly + 97000/12 yearly
        assert resp.json()["mrrCents"] == 9700 + 97000 // 12
    _clear()


async def test_invalid_coupon(db):
    staff = await _pro(db, "s2@x.com", is_staff=True)
    target = await _pro(db, "t@x.com")
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/admin/billing/professionals/{target.id}/apply-coupon",
            headers=_auth(staff),
            json={"code": "NOPE"},
        )
        assert resp.status_code == 404
    _clear()


async def test_coupon_discount_and_admin_apply(db):
    staff = await _pro(db, "s3@x.com", is_staff=True)
    target = await _pro(db, "t2@x.com", subscription_status="trial_expired")
    client = await _client(db)
    async with client:
        resp = await client.post(
            "/api/v1/admin/billing/coupons",
            headers=_auth(staff),
            json={
                "code": "SAVE10",
                "couponType": "percent",
                "value": 10,
                "trialBonusDays": 7,
            },
        )
        assert resp.status_code == 201

        resp = await client.post(
            f"/api/v1/admin/billing/professionals/{target.id}/apply-coupon",
            headers=_auth(staff),
            json={"code": "SAVE10"},
        )
        assert resp.status_code == 200
        assert resp.json()["trialExtendedDays"] == 7

    svc = CouponService(db)
    coupon = await svc.get_by_code("SAVE10")
    assert svc.discounted_price_cents(coupon, 9700) == 8730
    _clear()


async def test_non_staff_forbidden(db):
    user = await _pro(db, "ns@x.com")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/billing/subscriptions", headers=_auth(user))
        assert resp.status_code == 403
    _clear()
