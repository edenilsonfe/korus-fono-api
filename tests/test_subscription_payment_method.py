"""Payment metadata must survive the real checkout-to-admin path."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.api.v1.billing import _attach_checkout_to_subscription
from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.payment_methods import payment_method_from_payload
from app.models.billing import BillingEvent, Plan, Subscription
from app.services.billing_checkout_service import BillingCheckoutService
from app.services.billing_reconciliation_service import BillingReconciliationService
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS
from app.services.subscription_payment_method_service import recover_subscription_payment_method


@pytest.mark.asyncio
async def test_new_monthly_checkout_exposes_provider_method_in_admin(
    db_session, professional, auth_headers, api_client, monkeypatch,
):
    professional.cpf = "24971563792"
    professional.is_staff = True
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.commit()

    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"

    async def fake_request_json(method, url, **kwargs):
        if method == "POST" and url.endswith("/subscriptions"):
            return {"id": "sub_new", "status": "ACTIVE"}
        if method == "GET" and url.endswith("/subscriptions/sub_new/payments"):
            return {"data": [
                {
                    "id": "pay_new",
                    "status": "PENDING",
                    "billingType": "PIX",
                    "invoiceUrl": "https://sandbox.asaas.com/i/pay_new",
                },
            ]}
        if method == "PUT" and url.endswith("/subscriptions/sub_new"):
            return {"id": "sub_new", "status": "INACTIVE"}
        if method == "POST" and url.endswith("/payments/pay_new"):
            return {"id": "pay_new"}
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    monkeypatch.setattr(
        "app.api.v1.billing.BillingCustomerService.ensure_customer",
        AsyncMock(return_value="cus_existing"),
    )

    checkout = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers,
        json={"planSlug": plan.slug},
    )
    assert checkout.status_code == 200
    subscription = (await db_session.execute(select(Subscription))).scalar_one()
    assert subscription.status == "incomplete"

    listing = await api_client.get(
        "/api/v1/admin/professionals", headers=auth_headers,
    )
    assert listing.status_code == 200
    account = next(row for row in listing.json()["items"] if row["id"] == str(professional.id))
    assert account["paymentMethod"] == "pix"


@pytest.mark.asyncio
async def test_loading_existing_checkout_recovers_provider_payment_method(
    db_session, professional, monkeypatch,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id, plan_id=plan.id,
        provider="asaas", status="incomplete", external_checkout_id="pay_existing",
    )
    db_session.add(subscription)
    await db_session.commit()
    await db_session.refresh(subscription)
    original_updated_at = subscription.updated_at
    gateway = AsyncMock()
    gateway.get_payment.return_value = {
        "id": "pay_existing", "billingType": "CREDIT_CARD", "status": "PENDING",
    }
    monkeypatch.setattr(
        "app.services.billing_checkout_service.AsaasPaymentGateway", lambda: gateway,
    )

    session = await BillingCheckoutService(db_session).get_session(
        session_id="pay_existing", professional=professional,
    )

    await db_session.refresh(subscription)
    assert subscription.payment_method == "credit_card"
    assert subscription.updated_at == original_updated_at
    assert subscription.status == "incomplete"
    assert session["status"] == "pending"
    assert session["access_granted"] is False


@pytest.mark.asyncio
async def test_reconciliation_repairs_method_without_replaying_processed_payment(
    db_session, professional, monkeypatch,
):
    professional.subscription_status = "active"
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id, plan_id=plan.id,
        provider="asaas", status="active", external_checkout_id="pay_existing",
        external_subscription_id="sub_existing", last_payment_at=datetime.now(UTC),
    )
    payment = {
        "id": "pay_existing", "billingType": "CREDIT_CARD", "status": "RECEIVED",
        "subscription": "sub_existing", "paymentDate": "2026-09-03",
    }
    db_session.add_all([
        subscription,
        BillingEvent(
            professional_id=professional.id, provider="asaas", status="processed",
            external_event_id="asaas-PAYMENT_RECEIVED-pay_existing",
            event_type="payment.succeeded", payload=payment,
        ),
    ])
    await db_session.commit()
    gateway = AsyncMock()
    gateway.list_subscription_payments.return_value = [payment]
    monkeypatch.setattr(
        "app.services.billing_reconciliation_service.AsaasPaymentGateway", lambda: gateway,
    )
    apply_events = AsyncMock()
    monkeypatch.setattr(
        "app.services.saas_billing_service.SaasBillingService.apply_normalized_events",
        apply_events,
    )

    await BillingReconciliationService(db_session).reconcile_professional(professional.id)

    await db_session.refresh(subscription)
    assert subscription.payment_method == "credit_card"
    assert subscription.status == "active"
    assert await db_session.scalar(select(func.count()).select_from(BillingEvent)) == 1
    apply_events.assert_not_awaited()


@pytest.mark.parametrize("payment", [
    {"billingTypes": ["PIX", "CREDIT_CARD"]},
    {"billingType": "UNDEFINED"},
    {},
])
def test_allowed_or_missing_methods_are_not_treated_as_a_payment_choice(payment):
    assert payment_method_from_payload(payment) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_method,payment_id", [
    ("credit_card", "pay_current"),
    (None, "pay_other"),
])
async def test_recovery_preserves_known_methods_and_rejects_unrelated_charges(
    db_session, professional, existing_method, payment_id,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        provider="asaas",
        external_checkout_id="pay_current",
        payment_method=existing_method,
    )
    db_session.add(subscription)
    await db_session.commit()

    changed = await recover_subscription_payment_method(
        db_session, subscription, {"id": payment_id, "billingType": "PIX"},
    )

    assert changed is False
    await db_session.refresh(subscription)
    assert subscription.payment_method == existing_method


@pytest.mark.asyncio
async def test_recovery_does_not_overwrite_a_concurrently_replaced_checkout(
    db_session, professional,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        provider="asaas",
        external_checkout_id="pay_old",
    )
    db_session.add(subscription)
    await db_session.commit()
    await db_session.execute(
        update(Subscription)
        .where(Subscription.id == subscription.id)
        .values(external_checkout_id="pay_new", payment_method="credit_card")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    changed = await recover_subscription_payment_method(
        db_session, subscription, {"id": "pay_old", "billingType": "PIX"},
    )

    assert changed is False
    await db_session.refresh(subscription)
    assert subscription.payment_method == "credit_card"
    assert subscription.external_checkout_id == "pay_new"


@pytest.mark.asyncio
@pytest.mark.parametrize("session,expected", [
    ({"external_checkout_id": "pay_old"}, "credit_card"),
    ({"external_checkout_id": "checkout_new"}, None),
    ({"external_checkout_id": "pay_old", "payment_method": "pix"}, "pix"),
])
async def test_checkout_attachment_keeps_method_bound_to_current_charge(
    db_session, professional, session, expected,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        provider="asaas",
        external_checkout_id="pay_old",
        payment_method="credit_card",
    )
    db_session.add(subscription)
    await db_session.commit()

    await _attach_checkout_to_subscription(
        db_session,
        professional_id=professional.id,
        provider="asaas",
        session=session,
        billing_document="",
    )

    await db_session.refresh(subscription)
    assert subscription.payment_method == expected
