"""Reconciliation must not auto-activate stub subscriptions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.billing.errors import PaymentGatewayError
from app.billing.types import InternalBillingEventType
from app.core.security import create_access_token
from app.models.billing import BillingEvent, Plan, Subscription
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


@pytest.mark.asyncio
async def test_reconcile_reapplies_an_existing_received_payment_to_repair_local_state(
    api_client,
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="monthly-reconcile-repair@test.com",
        password_hash="hash",
        name="Monthly Reconcile Repair",
        subscription_status="active",
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="past_due",
        provider="asaas",
        external_subscription_id="sub_monthly_repair",
        external_checkout_id="pay_future_deleted",
        last_payment_at=datetime(2026, 8, 25, tzinfo=UTC),
        current_period_end=datetime(2026, 9, 25, tzinfo=UTC),
    )
    received_payment = {
        "id": "pay_already_received",
        "status": "RECEIVED",
        "value": 97.9,
        "billingType": "PIX",
        "paymentDate": "2026-08-25",
        "subscription": "sub_monthly_repair",
        "externalReference": (
            f"{professional.id}:korusfono_pro_monthly"
        ),
    }
    existing_event = BillingEvent(
        provider="asaas",
        external_event_id="asaas-PAYMENT_RECEIVED-pay_already_received",
        event_type=InternalBillingEventType.PAYMENT_SUCCEEDED.value,
        payload={
            **received_payment,
            "provider": "asaas",
            "external_subscription_id": "sub_monthly_repair",
            "external_checkout_id": "pay_already_received",
            "subscription_status": "active",
        },
        status="processed",
        professional_id=professional.id,
        processed_at=datetime(2026, 8, 25, 10, 7, tzinfo=UTC),
        created_at=datetime(2026, 8, 25, 10, 7, tzinfo=UTC),
    )
    db_session.add_all([subscription, existing_event])
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_subscription_payments = AsyncMock(
        return_value=[received_payment]
    )
    gateway.get_payment = AsyncMock(
        return_value={
            "id": "pay_future_deleted",
            "status": "PENDING",
            "deleted": True,
            "dueDate": "2026-09-26",
            "subscription": "sub_monthly_repair",
        }
    )

    token = create_access_token(professional.id)
    with patch(
        "app.services.billing_reconciliation_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        response = await api_client.post(
            "/api/v1/billing/reconcile",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["subscriptionStatus"] == "active"
    assert response.json()["professionalStatus"] == "active"

    billing_me = await api_client.get(
        "/api/v1/billing/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert billing_me.status_code == 200
    assert billing_me.json()["subscriptionStatus"] == "active"
    assert billing_me.json()["subscription"]["status"] == "active"
    assert billing_me.json()["subscription"]["lastPaymentAt"].startswith(
        "2026-08-25T00:00:00"
    )
    assert billing_me.json()["subscription"]["currentPeriodEnd"].startswith(
        "2026-09-25T00:00:00"
    )
    await db_session.refresh(subscription)
    assert subscription.payment_method == "pix"
