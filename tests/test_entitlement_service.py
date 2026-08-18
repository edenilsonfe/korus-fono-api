"""Entitlement / trial write access tests."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS


@pytest.mark.asyncio
async def test_can_write_trialing_future(db_session):
    professional = Professional(
        email="trial@test.com",
        password_hash="hash",
        name="Trial User",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC) + timedelta(days=3),
    )
    db_session.add(professional)
    await db_session.commit()

    svc = EntitlementService(db_session)
    assert await svc.can_write(professional) is True


@pytest.mark.asyncio
async def test_can_write_trialing_expired_sets_trial_expired(db_session):
    professional = Professional(
        email="expired@test.com",
        password_hash="hash",
        name="Expired User",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC) - timedelta(days=10),
        trial_ends_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(professional)
    await db_session.commit()

    svc = EntitlementService(db_session)
    assert await svc.can_write(professional) is False
    assert professional.subscription_status == "trial_expired"


@pytest.mark.asyncio
async def test_can_write_active(db_session):
    professional = Professional(
        email="active@test.com",
        password_hash="hash",
        name="Active User",
        subscription_status="active",
    )
    db_session.add(professional)
    await db_session.commit()

    svc = EntitlementService(db_session)
    assert await svc.can_write(professional) is True


@pytest.mark.asyncio
async def test_expired_non_recurring_annual_purchase_blocks_writes(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-expired@test.com",
        password_hash="hash",
        name="Annual Expired",
        subscription_status="active",
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="active",
        provider="asaas",
        external_subscription_id=None,
        external_checkout_id="chk_annual_expired",
        current_period_end=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(subscription)
    await db_session.commit()

    assert await EntitlementService(db_session).can_write(professional) is False

    await db_session.refresh(subscription)
    await db_session.refresh(professional)
    assert subscription.status == "expired"
    assert professional.subscription_status == "past_due"


@pytest.mark.asyncio
async def test_non_recurring_annual_purchase_remains_active_before_period_end(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-active@test.com",
        password_hash="hash",
        name="Annual Active",
        subscription_status="active",
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            status="active",
            provider="asaas",
            external_subscription_id=None,
            external_checkout_id="chk_annual_active",
            current_period_end=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.commit()

    assert await EntitlementService(db_session).can_write(professional) is True
