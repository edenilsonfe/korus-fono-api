"""Billing webhook and reconciliation tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.billing.errors import PaymentGatewayError
from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import StubWebhookNormalizer
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.billing_checkout_service import BillingCheckoutService
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS
from app.services.saas_billing_service import SaasBillingService


@pytest.mark.asyncio
async def test_checkout_returns_bad_gateway_when_asaas_rejects_customer(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(
        side_effect=PaymentGatewayError(
            'Gateway HTTP 401: {"errors":[{"code":"invalid_environment"}]}',
            status_code=401,
        )
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={"planSlug": plan.slug},
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Não foi possível iniciar o pagamento. Tente novamente em instantes."
    }


@pytest.mark.asyncio
async def test_stub_webhook_activates_subscription(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="billing@test.com",
        password_hash="hash",
        name="Billing User",
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
        external_subscription_id="stub_sub_test",
        external_checkout_id="stub_pay_test",
    )
    db_session.add(sub)
    await db_session.commit()

    professional_id = str(professional.id)
    normalizer = StubWebhookNormalizer()
    events = normalizer.normalize(
        {
            "id": "evt-1",
            "event_type": InternalBillingEventType.PAYMENT_SUCCEEDED.value,
            "professional_id": professional_id,
            "plan_slug": plan.slug,
            "provider": "stub",
            "external_subscription_id": "stub_sub_test",
            "subscription_status": "active",
        },
        {},
    )

    billing = SaasBillingService(db_session)
    row = await billing.record_webhook_raw(
        provider="stub",
        external_event_id=events[0].external_event_id,
        event_type=events[0].event_type.value,
        payload=events[0].payload,
        professional_id=professional_id,
    )
    assert row is not None
    await billing.apply_normalized_events(events)

    await db_session.refresh(professional)
    await db_session.refresh(sub)
    assert professional.subscription_status == "active"
    assert sub.status == "active"


@pytest.mark.asyncio
async def test_get_session_stub_has_null_invoice_url(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="invoice-stub@test.com",
        password_hash="hash",
        name="Stub Invoice User",
        cpf="24971563792",
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
        external_subscription_id="stub_sub_invoice",
        external_checkout_id="stub_pay_invoice",
    )
    db_session.add(sub)
    await db_session.commit()

    service = BillingCheckoutService(db_session)
    session = await service.get_session(
        session_id="stub_pay_invoice", professional=professional
    )
    assert session["provider"] == "stub"
    assert session["invoice_url"] is None
    assert session["status"] == "pending"


@pytest.mark.asyncio
async def test_get_session_asaas_exposes_invoice_url(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="invoice-asaas@test.com",
        password_hash="hash",
        name="Asaas Invoice User",
        cpf="24971563792",
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
        external_subscription_id="sub_asaas_invoice",
        external_checkout_id="pay_asaas_invoice",
    )
    db_session.add(sub)
    await db_session.commit()

    invoice = "https://sandbox.asaas.com/i/pay_asaas_invoice"
    gateway = AsyncMock()
    gateway.get_payment = AsyncMock(
        return_value={
            "id": "pay_asaas_invoice",
            "status": "PENDING",
            "value": 97.9,
            "invoiceUrl": invoice,
        }
    )

    service = BillingCheckoutService(db_session)
    with patch(
        "app.services.billing_checkout_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        session = await service.get_session(
            session_id="pay_asaas_invoice", professional=professional
        )

    assert session["invoice_url"] == invoice
    assert session["status"] == "pending"
    assert session["charge_cents"] == 9790


@pytest.mark.asyncio
async def test_credit_card_pan_route_removed():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/billing/checkout/any/credit-card",
            json={
                "holderName": "X",
                "number": "5162306219378829",
                "expiryMonth": "05",
                "expiryYear": "2030",
                "ccv": "123",
                "postalCode": "01310100",
                "addressNumber": "100",
                "phone": "11999990000",
            },
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_prepare_card_invoice_flips_pix_to_credit_card(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="prepare-card@test.com",
        password_hash="hash",
        name="Prepare Card User",
        cpf="24971563792",
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
        external_subscription_id="sub_prepare_card",
        external_checkout_id="pay_prepare_card",
    )
    db_session.add(sub)
    await db_session.commit()

    invoice = "https://sandbox.asaas.com/i/pay_prepare_card"
    gateway = AsyncMock()
    gateway.ensure_card_billing = AsyncMock(
        return_value={
            "id": "pay_prepare_card",
            "billingType": "CREDIT_CARD",
            "invoiceUrl": invoice,
        }
    )

    service = BillingCheckoutService(db_session)
    with patch(
        "app.services.billing_checkout_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        result = await service.prepare_card_invoice(
            session_id="pay_prepare_card", professional=professional
        )

    assert result["invoice_url"] == invoice
    gateway.ensure_card_billing.assert_awaited_once_with("pay_prepare_card")
