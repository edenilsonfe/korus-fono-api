"""Tests for admin billing metrics, coupons and plans."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

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
from app.schemas.admin_billing import CouponCreate, CouponUpdate
from app.services.coupon_service import CouponError, CouponService

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


async def test_coupon_reservation_payment_refund_and_first_purchase(db, monkeypatch):
    async def no_referral(_self, _professional_id):
        return 0, None

    monkeypatch.setattr("app.services.coupon_service.AffiliateService.referral_discount", no_referral)
    async def no_credit(_self, _professional_id):
        return 0
    monkeypatch.setattr("app.services.coupon_service.AffiliateCreditService.credit_balance", no_credit)
    staff = await _pro(db, "coupon-staff@x.com", is_staff=True)
    first = await _pro(db, "coupon-first@x.com")
    second = await _pro(db, "coupon-second@x.com")
    plan = db.info["plan"]
    service = CouponService(db)
    today = date.today()
    await service.create(
        actor=staff,
        body=CouponCreate(
            code="FIRST10",
            coupon_type="percent",
            value=10,
            valid_from=today,
            valid_until=today,
            max_redemptions=1,
        ),
    )
    coupon = await service.get_by_code("FIRST10")
    await service.update(actor=staff, coupon_id=coupon.id, body=CouponUpdate(code="FIRST10B"))
    await service.update(actor=staff, coupon_id=coupon.id, body=CouponUpdate(code="FIRST10"))
    quote = await service.preview(code="FIRST10", plan=plan, professional=first)
    assert quote["first_charge_cents"] == 8730
    assert quote["renewal_cents"] == 9700

    sub = Subscription(
        professional_id=first.id,
        plan_id=plan.id,
        status="incomplete",
        checkout_session_id=uuid4(),
    )
    db.add(sub)
    await db.flush()
    reservation = await service.reserve(
        coupon=coupon,
        professional_id=first.id,
        subscription_id=sub.id,
        checkout_session_id=sub.checkout_session_id,
        plan_slug=plan.slug,
        discounted_price_cents=8730,
    )
    await service.bind_payment(reservation.id, "pay-first")
    with pytest.raises(CouponError, match="não podem mudar"):
        await service.update(actor=staff, coupon_id=coupon.id, body=CouponUpdate(code="TOO-LATE"))
    await service.update(actor=staff, coupon_id=coupon.id, body=CouponUpdate(is_active=False))
    assert (await service.reserve(
        coupon=coupon, professional_id=first.id, subscription_id=sub.id,
        checkout_session_id=sub.checkout_session_id, plan_slug=plan.slug,
        discounted_price_cents=8730,
    )).id == reservation.id
    await service.update(actor=staff, coupon_id=coupon.id, body=CouponUpdate(is_active=True))
    assert (await service.list_coupons())[0].reserved_count == 1
    with pytest.raises(CouponError, match="esgotado"):
        await service.preview(code="FIRST10", plan=plan, professional=second)

    replacement = await service.reserve(
        coupon=coupon,
        professional_id=first.id,
        subscription_id=sub.id,
        checkout_session_id=uuid4(),
        plan_slug=plan.slug,
        discounted_price_cents=8730,
        allow_replacement=True,
    )
    await service.release(reservation.id)
    await service.bind_payment(replacement.id, "pay-new")
    assert (await service.list_coupons())[0].reserved_count == 1

    await service.apply_payment_event(
        subscription=sub,
        payment_ids={"pay-new"},
        provider_event="PAYMENT_CONFIRMED",
    )
    assert (await service.list_coupons())[0].redemption_count == 1
    await service.apply_payment_event(
        subscription=sub,
        payment_ids={"pay-new"},
        provider_event="PAYMENT_CHARGEBACK_REQUESTED",
    )
    assert (await service.list_coupons())[0].redemption_count == 1
    await service.apply_payment_event(
        subscription=sub,
        payment_ids={"pay-new"},
        provider_event="PAYMENT_REFUNDED",
    )
    assert (await service.list_coupons())[0].redemption_count == 0
    with pytest.raises(CouponError, match="já utilizou"):
        await service.preview(code="FIRST10", plan=plan, professional=first)
    assert (await service.preview(code="FIRST10", plan=plan, professional=second))["first_charge_cents"] == 8730
    annual = Subscription(
        professional_id=second.id, plan_id=plan.id, status="incomplete",
        checkout_session_id=uuid4(),
    )
    db.add(annual)
    await db.flush()
    annual_reservation = await service.reserve(
        coupon=coupon, professional_id=second.id, subscription_id=annual.id,
        checkout_session_id=annual.checkout_session_id, plan_slug=plan.slug,
        discounted_price_cents=8730,
    )
    await service.bind_payment(annual_reservation.id, "chk-annual")
    await service.apply_payment_event(
        subscription=annual, payment_ids={"chk-annual"}, provider_event="CHECKOUT_PAID",
    )
    assert (await service.list_coupons())[0].redemption_count == 1
    _clear()


async def test_expired_coupon_reservation_cancels_provider_before_release(db, monkeypatch):
    staff = await _pro(db, "expiry-staff@x.com", is_staff=True)
    customer = await _pro(db, "expiry-customer@x.com")
    plan = db.info["plan"]
    service = CouponService(db)
    await service.create(actor=staff, body=CouponCreate(
        code="EXPIRE10", coupon_type="percent", value=10, max_redemptions=1,
    ))
    coupon = await service.get_by_code("EXPIRE10")
    sub = Subscription(
        professional_id=customer.id, plan_id=plan.id, status="incomplete",
        provider="asaas", external_subscription_id="sub_pending",
        external_checkout_id="pay_pending", checkout_session_id=uuid4(),
    )
    db.add(sub)
    await db.flush()
    reservation = await service.reserve(
        coupon=coupon, professional_id=customer.id, subscription_id=sub.id,
        checkout_session_id=sub.checkout_session_id, plan_slug=plan.slug,
        discounted_price_cents=8730,
    )
    await service.bind_payment(reservation.id, "pay_pending")
    reservation.reserved_until = utcnow() - timedelta(minutes=1)
    await db.commit()
    gateway = SimpleNamespace(
        get_payment=AsyncMock(return_value={"status": "PENDING"}),
        cancel_subscription=AsyncMock(),
    )
    monkeypatch.setattr("app.services.coupon_service.AsaasPaymentGateway", lambda: gateway)

    assert await service.expire_due() == 1
    gateway.cancel_subscription.assert_awaited_once_with(external_subscription_id="sub_pending")
    assert reservation.state == "released"
    assert sub.status == "canceled"
    assert (await service.list_coupons())[0].reserved_count == 0
    _clear()


async def test_non_staff_forbidden(db):
    user = await _pro(db, "ns@x.com")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/billing/subscriptions", headers=_auth(user))
        assert resp.status_code == 403
    _clear()
