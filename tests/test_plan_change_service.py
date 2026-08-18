from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS
from app.services.plan_change_service import PlanChangeService


async def _monthly_subscription_with_annual_target(db_session, *, pending: bool = False):
    monthly = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    yearly = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email=f"plan-change-{pending}@test.com",
        password_hash="hash",
        name="Plan Change",
        cpf="24971563792",
        subscription_status="active",
    )
    db_session.add_all([monthly, yearly, professional])
    await db_session.flush()
    now = datetime.now(UTC)
    sub = Subscription(
        professional_id=professional.id,
        plan_id=monthly.id,
        pending_plan_id=yearly.id if pending else None,
        status="active",
        provider="asaas",
        external_subscription_id="sub_monthly_recurring",
        external_checkout_id="chk_upgrade_annual" if pending else "pay_monthly",
        started_at=now - timedelta(days=10),
        last_payment_at=now - timedelta(days=10),
        current_period_end=now + timedelta(days=20),
    )
    db_session.add(sub)
    await db_session.commit()
    return professional, monthly, yearly, sub


@pytest.mark.asyncio
async def test_monthly_to_yearly_upgrade_uses_hosted_installment_checkout(db_session):
    professional, _monthly, yearly, sub = await _monthly_subscription_with_annual_target(
        db_session
    )
    gateway = AsyncMock()
    gateway.create_hosted_annual_checkout = AsyncMock(
        return_value={
            "id": "chk_upgrade_annual",
            "status": "ACTIVE",
            "link": "https://sandbox.asaas.com/checkoutSession/show?id=chk_upgrade_annual",
        }
    )

    with patch(
        "app.services.plan_change_service.BillingCustomerService.ensure_customer",
        new=AsyncMock(return_value="cus_upgrade"),
    ):
        result = await PlanChangeService(db_session, gateway).initiate_change(
            professional=professional,
            subscription=sub,
            target_plan=yearly,
            document="24971563792",
            provider="asaas",
        )

    assert result["session_id"] == "chk_upgrade_annual"
    assert result["status"] == "pending"
    call = gateway.create_hosted_annual_checkout.await_args.kwargs
    assert call["customer_id"] == "cus_upgrade"
    assert call["plan_slug"] == yearly.slug
    assert 0 < call["value_cents"] < yearly.price_cents
    assert call["external_reference"].endswith(":upgrade")
    gateway.create_single_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_annual_upgrade_cancels_monthly_renewal_and_grants_twelve_months(
    db_session,
):
    professional, _monthly, yearly, sub = await _monthly_subscription_with_annual_target(
        db_session, pending=True
    )
    gateway = AsyncMock()
    gateway.get_checkout = AsyncMock(
        return_value={"id": "chk_upgrade_annual", "status": "PAID"}
    )
    gateway.cancel_subscription = AsyncMock(return_value={"status": "canceled"})

    before = datetime.now(UTC)
    applied = await PlanChangeService(db_session, gateway).apply_pending_upgrade(
        professional.id
    )

    assert applied is True
    await db_session.refresh(sub)
    await db_session.refresh(professional)
    assert sub.plan_id == yearly.id
    assert sub.pending_plan_id is None
    assert sub.external_subscription_id is None
    assert sub.external_checkout_id == "chk_upgrade_annual"
    assert sub.status == "active"
    assert professional.subscription_status == "active"
    assert sub.current_period_end >= before.replace(tzinfo=None) + timedelta(days=364)
    gateway.cancel_subscription.assert_awaited_once_with(
        external_subscription_id="sub_monthly_recurring"
    )
    gateway.update_subscription_plan.assert_not_awaited()
