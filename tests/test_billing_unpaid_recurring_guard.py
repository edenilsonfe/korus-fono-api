"""Guards against recurring Asaas invoices before the first confirmed payment."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayError
from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import NormalizedBillingEvent
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS
from app.services.saas_billing_service import SaasBillingService


@pytest.mark.asyncio
async def test_pending_monthly_checkout_suspends_provider_subscription(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json_body")))
        if method == "POST" and url.endswith("/subscriptions"):
            return {"id": "sub_unpaid_monthly", "status": "ACTIVE"}
        if method == "GET" and url.endswith(
            "/subscriptions/sub_unpaid_monthly/payments"
        ):
            return {
                "data": [
                    {
                        "id": "pay_first_monthly",
                        "status": "PENDING",
                        "invoiceUrl": "https://sandbox.asaas.com/i/pay_first_monthly",
                    }
                ]
            }
        if method == "PUT" and url.endswith("/subscriptions/sub_unpaid_monthly"):
            return {"id": "sub_unpaid_monthly", "status": "INACTIVE"}
        if method == "POST" and url.endswith("/payments/pay_first_monthly"):
            return {"id": "pay_first_monthly"}
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    session = await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="korusfono_pro_monthly",
        success_url="https://app.test/retorno",
        cancel_url="https://app.test/planos",
        metadata={
            "price_cents": 9790,
            "plan_name": "KorusFono Pro",
            "billing_interval": "monthly",
            "customer_external_id": "cus_existing",
        },
    )

    assert session["status"] == "pending"
    subscription_creation = next(
        body
        for method, url, body in calls
        if method == "POST" and url.endswith("/subscriptions")
    )
    assert subscription_creation["billingType"] == "PIX"
    assert (
        "PUT",
        "https://api-sandbox.asaas.com/v3/subscriptions/sub_unpaid_monthly",
        {"status": "INACTIVE"},
    ) in calls


@pytest.mark.asyncio
async def test_pending_monthly_card_suspends_provider_subscription(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json_body")))
        if method == "POST" and url.endswith("/subscriptions"):
            return {"id": "sub_card_pending", "status": "ACTIVE"}
        if method == "GET" and url.endswith(
            "/subscriptions/sub_card_pending/payments"
        ):
            return {
                "data": [
                    {
                        "id": "pay_card_pending",
                        "status": "AWAITING_RISK_ANALYSIS",
                    }
                ]
            }
        if method == "PUT" and url.endswith("/subscriptions/sub_card_pending"):
            return {"id": "sub_card_pending", "status": "INACTIVE"}
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    result = await gateway.create_credit_card_subscription(
        customer_id="cus_card",
        account_id="account-1",
        plan_slug="korusfono_pro_monthly",
        plan_name="KorusFono Pro",
        value_cents=9790,
        checkout_reference="checkout-1",
        credit_card={
            "holderName": "Maria da Silva",
            "number": "4111111111111111",
            "expiryMonth": "05",
            "expiryYear": "2030",
            "ccv": "123",
        },
        holder_info={
            "name": "Maria da Silva",
            "email": "maria@example.com",
            "cpfCnpj": "24971563792",
            "postalCode": "01310100",
            "addressNumber": "100",
            "phone": "11999990000",
            "mobilePhone": "11999990000",
        },
        remote_ip="203.0.113.10",
    )

    assert result["payment"]["status"] == "AWAITING_RISK_ANALYSIS"
    assert (
        "PUT",
        "https://api-sandbox.asaas.com/v3/subscriptions/sub_card_pending",
        {"status": "INACTIVE"},
    ) in calls


@pytest.mark.asyncio
async def test_asaas_activation_sets_status_and_next_due_date(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "PUT"
        assert url.endswith("/subscriptions/sub_activate")
        captured.update(kwargs["json_body"])
        return {"id": "sub_activate", "status": "ACTIVE"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    result = await gateway.activate_subscription(
        external_subscription_id="sub_activate",
        next_due_date="2026-10-02",
    )

    assert captured == {"status": "ACTIVE", "nextDueDate": "2026-10-02"}
    assert result == {
        "status": "active",
        "external_subscription_id": "sub_activate",
        "next_due_date": "2026-10-02",
    }


@pytest.mark.asyncio
async def test_failed_suspension_deletes_new_recurring_subscription(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url))
        if method == "PUT":
            raise PaymentGatewayError("Asaas indisponível", status_code=503)
        if method == "DELETE":
            return {"deleted": True}
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    with pytest.raises(PaymentGatewayError):
        await gateway._suspend_until_first_payment(
            "sub_suspend_failed",
            {"id": "pay_pending", "status": "PENDING"},
        )

    assert (
        "DELETE",
        "https://api-sandbox.asaas.com/v3/subscriptions/sub_suspend_failed",
    ) in calls


@pytest.mark.asyncio
async def test_deleted_first_payment_cancels_never_paid_provider_subscription(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="expired-unpaid@test.com",
        password_hash="hash",
        name="Expired Unpaid",
        subscription_status="trial_expired",
        trial_ends_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id="sub_never_paid",
        external_checkout_id="pay_first_deleted",
    )
    db_session.add(subscription)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_subscription_payments = AsyncMock(return_value=[])
    gateway.cancel_subscription = AsyncMock(return_value={"status": "canceled"})
    event = NormalizedBillingEvent(
        event_type=InternalBillingEventType.PAYMENT_DELETED,
        external_event_id="asaas-PAYMENT_DELETED-pay_first_deleted",
        payload={
            "id": "pay_first_deleted",
            "provider": "asaas",
            "provider_event": "PAYMENT_DELETED",
            "professional_id": str(professional.id),
            "external_subscription_id": "sub_never_paid",
            "external_checkout_id": "pay_first_deleted",
            "value": 97.9,
        },
        professional_hint=str(professional.id),
    )

    with patch(
        "app.services.saas_billing_service.AsaasPaymentGateway",
        return_value=gateway,
        create=True,
    ):
        await SaasBillingService(db_session).apply_normalized_events([event])

    gateway.cancel_subscription.assert_awaited_once_with(
        external_subscription_id="sub_never_paid"
    )
    await db_session.refresh(subscription)
    await db_session.refresh(professional)
    assert subscription.status == "canceled"
    assert professional.subscription_status == "trial_expired"


@pytest.mark.asyncio
async def test_deleted_payment_does_not_cancel_when_provider_has_a_paid_cycle(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="provider-paid@test.com",
        password_hash="hash",
        name="Provider Paid",
        subscription_status="past_due",
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id="sub_provider_paid",
        external_checkout_id="pay_deleted_future",
    )
    db_session.add(subscription)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_subscription_payments = AsyncMock(
        return_value=[{"id": "pay_received", "status": "RECEIVED"}]
    )
    gateway.cancel_subscription = AsyncMock()
    event = NormalizedBillingEvent(
        event_type=InternalBillingEventType.PAYMENT_DELETED,
        external_event_id="asaas-PAYMENT_DELETED-pay_deleted_future",
        payload={
            "id": "pay_deleted_future",
            "provider": "asaas",
            "external_subscription_id": "sub_provider_paid",
            "external_checkout_id": "pay_deleted_future",
        },
        professional_hint=str(professional.id),
    )

    with patch(
        "app.services.saas_billing_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        await SaasBillingService(db_session).apply_normalized_events([event])

    gateway.cancel_subscription.assert_not_awaited()
    await db_session.refresh(subscription)
    assert subscription.status == "incomplete"


@pytest.mark.asyncio
async def test_confirmed_payment_reactivates_suspended_monthly_subscription(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="paid-after-suspension@test.com",
        password_hash="hash",
        name="Paid After Suspension",
        subscription_status="trial_expired",
        trial_ends_at=datetime(2026, 8, 18, tzinfo=UTC),
        signup_payment_required=True,
        email_verified_at=datetime.now(UTC),
        temporary_access_ends_at=datetime.now(UTC) + timedelta(days=2),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id="sub_suspended_paid",
        external_checkout_id="pay_confirmed",
    )
    db_session.add(subscription)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.get_subscription_status = AsyncMock(
        return_value={
            "status": "inactive",
            "external_subscription_id": "sub_suspended_paid",
        }
    )
    gateway.activate_subscription = AsyncMock(return_value={"status": "active"})
    payment_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    event = NormalizedBillingEvent(
        event_type=InternalBillingEventType.PAYMENT_SUCCEEDED,
        external_event_id="asaas-PAYMENT_RECEIVED-pay_confirmed",
        payload={
            "id": "pay_confirmed",
            "provider": "asaas",
            "provider_event": "PAYMENT_RECEIVED",
            "professional_id": str(professional.id),
            "plan_slug": plan.slug,
            "external_subscription_id": "sub_suspended_paid",
            "external_checkout_id": "pay_confirmed",
            "last_payment_at": payment_at.isoformat(),
            "subscription_status": "active",
            "value": 97.9,
        },
        professional_hint=str(professional.id),
    )

    with (
        patch(
            "app.services.saas_billing_service.AsaasPaymentGateway",
            return_value=gateway,
            create=True,
        ),
        patch(
            "app.services.saas_billing_service.AffiliateService.record_external_payment",
            new=AsyncMock(),
        ),
    ):
        await SaasBillingService(db_session).apply_normalized_events([event])

    gateway.activate_subscription.assert_awaited_once_with(
        external_subscription_id="sub_suspended_paid",
        next_due_date=date(2026, 10, 2).isoformat(),
    )
    await db_session.refresh(subscription)
    await db_session.refresh(professional)
    assert subscription.status == "active"
    assert professional.subscription_status == "active"
    assert professional.signup_payment_required is False
    assert professional.temporary_access_ends_at is None
