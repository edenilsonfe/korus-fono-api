"""Reconciliation must not auto-activate stub subscriptions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.billing.errors import PaymentGatewayError
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.billing_reconciliation_service import BillingReconciliationService
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS


@pytest.mark.asyncio
async def test_reconcile_stub_does_not_activate_without_payment(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="stub-reconcile@test.com",
        password_hash="hash",
        name="Stub User",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()

    sub = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="stub",
        external_subscription_id="stub_sub_x",
        external_checkout_id="stub_pay_x",
    )
    db_session.add(sub)
    await db_session.commit()

    service = BillingReconciliationService(db_session)
    result = await service.reconcile_professional(professional.id)

    assert result["applied"] is False
    await db_session.refresh(professional)
    await db_session.refresh(sub)
    assert professional.subscription_status == "trialing"
    assert sub.status == "incomplete"


@pytest.mark.asyncio
async def test_simulate_stub_payment_activates_explicitly(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="stub-simulate@test.com",
        password_hash="hash",
        name="Stub Simulate",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()

    sub = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="stub",
        external_subscription_id="stub_sub_y",
        external_checkout_id="stub_pay_y",
    )
    db_session.add(sub)
    await db_session.commit()

    service = BillingReconciliationService(db_session)
    result = await service.simulate_stub_payment(professional.id)

    assert result["applied"] is True
    await db_session.refresh(professional)
    await db_session.refresh(sub)
    assert professional.subscription_status == "active"
    assert sub.status == "active"


@pytest.mark.asyncio
async def test_reconcile_paid_annual_checkout_activates_twelve_month_access(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-reconcile@test.com",
        password_hash="hash",
        name="Annual Reconcile",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    sub = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id=None,
        external_checkout_id="chk_paid_annual",
    )
    db_session.add(sub)
    await db_session.commit()

    paid_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    gateway = AsyncMock()
    gateway.get_payment = AsyncMock(
        side_effect=PaymentGatewayError("Cobrança não encontrada", status_code=404)
    )
    gateway.list_checkout_payments = AsyncMock(
        return_value=[
            {
                "id": "pay_paid_annual",
                "status": "CONFIRMED",
                "value": 970.0,
                "checkoutSession": "chk_paid_annual",
                "paymentDate": paid_at.isoformat(),
            }
        ]
    )

    with patch(
        "app.services.billing_reconciliation_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        result = await BillingReconciliationService(db_session).reconcile_professional(
            professional.id
        )

    assert result["applied"] is True
    await db_session.refresh(professional)
    await db_session.refresh(sub)
    assert professional.subscription_status == "active"
    assert sub.status == "active"
    assert sub.external_subscription_id is None
    assert sub.external_checkout_id == "chk_paid_annual"
    assert sub.current_period_end == datetime(2027, 8, 18, 12, 0)
