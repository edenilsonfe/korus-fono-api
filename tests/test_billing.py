"""Billing webhook and reconciliation tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.billing.errors import PaymentGatewayError
from app.billing.asaas_gateway import AsaasPaymentGateway
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
async def test_checkout_saves_cpf_and_cnpj_separately_and_uses_selected_cnpj(
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
    gateway.create_customer = AsyncMock(return_value={"external_customer_id": "cus_cnpj"})
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "https://sandbox.asaas.com/i/pay_cnpj",
            "session_id": "pay_cnpj",
            "status": "pending",
        }
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "billingDocumentType": "cnpj",
            "cnpj": "11.222.333/0001-81",
        },
    )

    assert response.status_code == 200
    await db_session.refresh(professional)
    assert professional.cpf == "24971563792"
    assert professional.billing_cnpj == "11222333000181"
    assert professional.billing_document_type == "cnpj"
    assert gateway.create_customer.await_args.kwargs["metadata"]["customer_document"] == (
        "11222333000181"
    )
    assert gateway.create_checkout_session.await_args.kwargs["metadata"][
        "customer_document"
    ] == "11222333000181"
    billing_me = await api_client.get("/api/v1/billing/me", headers=auth_headers)
    assert billing_me.status_code == 200
    assert billing_me.json()["billingCpf"] == "24971563792"
    assert billing_me.json()["billingCnpj"] == "11222333000181"
    assert billing_me.json()["billingDocumentType"] == "cnpj"
    assert billing_me.json()["billingDocument"] == "11222333000181"

    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.professional_id == professional.id)
    )
    assert subscription is not None
    assert subscription.billing_document == "11222333000181"

    switch_to_cpf = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "billingDocumentType": "cpf",
            "cpf": "249.715.637-92",
        },
    )
    assert switch_to_cpf.status_code == 200
    await db_session.refresh(professional)
    assert professional.cpf == "24971563792"
    assert professional.billing_cnpj == "11222333000181"
    assert professional.billing_document_type == "cpf"

    billing_me_after_switch = await api_client.get("/api/v1/billing/me", headers=auth_headers)
    assert billing_me_after_switch.json()["billingCpf"] == "24971563792"
    assert billing_me_after_switch.json()["billingCnpj"] == "11222333000181"
    assert billing_me_after_switch.json()["billingDocument"] == "24971563792"


@pytest.mark.asyncio
async def test_checkout_rejects_invalid_billing_document(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "billingDocumentType": "cnpj",
            "cnpj": "11.222.333/0001-82",
        },
    )

    assert response.status_code == 422
    assert "CNPJ inválido" in response.json()["detail"][0]["msg"]
    gateway.create_customer.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_marks_pending_charge_for_replacement_when_document_changes(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            status="incomplete",
            provider="asaas",
            external_subscription_id="sub_cpf_pending",
            external_checkout_id="pay_cpf_pending",
            billing_document="24971563792",
        )
    )
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(return_value={"external_customer_id": "cus_existing"})
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "https://sandbox.asaas.com/i/pay_cnpj_new",
            "session_id": "pay_cnpj_new",
            "external_subscription_id": "sub_cnpj_new",
            "status": "pending",
        }
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "billingDocumentType": "cnpj",
            "cnpj": "11.222.333/0001-81",
        },
    )

    assert response.status_code == 200
    metadata = gateway.create_checkout_session.await_args.kwargs["metadata"]
    assert metadata["existing_external_subscription_id"] == "sub_cpf_pending"
    assert metadata["existing_external_checkout_id"] == "pay_cpf_pending"
    assert metadata["replace_existing_checkout"] is True

    retry = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "billingDocumentType": "cnpj",
            "cnpj": "11.222.333/0001-81",
        },
    )
    assert retry.status_code == 200
    retry_metadata = gateway.create_checkout_session.await_args.kwargs["metadata"]
    assert "replace_existing_checkout" not in retry_metadata


@pytest.mark.asyncio
async def test_asaas_replaces_pending_subscription_before_creating_charge_for_new_document(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url))
        if method == "PUT" and url.endswith("/customers/cus_existing"):
            return {"id": "cus_existing"}
        if method == "GET" and url.endswith("/payments/pay_cpf_pending"):
            return {
                "id": "pay_cpf_pending",
                "status": "PENDING",
                "externalReference": "account-1:pro-mensal",
            }
        if method == "DELETE" and url.endswith("/subscriptions/sub_cpf_pending"):
            return {"deleted": True}
        if method == "POST" and url.endswith("/subscriptions"):
            return {"id": "sub_cnpj_new"}
        if method == "GET" and url.endswith("/subscriptions/sub_cnpj_new/payments"):
            return {
                "data": [
                    {
                        "id": "pay_cnpj_new",
                        "status": "PENDING",
                        "invoiceUrl": "https://sandbox.asaas.com/i/pay_cnpj_new",
                    }
                ]
            }
        if method == "POST" and url.endswith("/payments/pay_cnpj_new"):
            return {"id": "pay_cnpj_new"}
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    session = await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="pro-mensal",
        success_url="https://app.test/retorno",
        cancel_url="https://app.test/planos",
        metadata={
            "price_cents": 9790,
            "plan_name": "KorusFono Pro",
            "billing_interval": "monthly",
            "customer_external_id": "cus_existing",
            "customer_document": "11222333000181",
            "existing_external_subscription_id": "sub_cpf_pending",
            "existing_external_checkout_id": "pay_cpf_pending",
            "replace_existing_checkout": True,
        },
    )

    assert session["external_subscription_id"] == "sub_cnpj_new"
    assert session["external_checkout_id"] == "pay_cnpj_new"
    assert (
        "DELETE",
        "https://api-sandbox.asaas.com/v3/subscriptions/sub_cpf_pending",
    ) in calls
    assert not any(
        method == "POST" and url.endswith("/subscriptions/sub_cpf_pending")
        for method, url in calls
    )


@pytest.mark.asyncio
async def test_asaas_does_not_replace_charge_that_was_paid_during_document_change(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url))
        if method == "PUT" and url.endswith("/customers/cus_existing"):
            return {"id": "cus_existing"}
        if method == "GET" and url.endswith("/payments/pay_cpf_pending"):
            return {
                "id": "pay_cpf_pending",
                "status": "CONFIRMED",
                "externalReference": "account-1:pro-mensal",
            }
        raise AssertionError(f"Unexpected Asaas call: {method} {url}")

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    session = await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="pro-mensal",
        success_url="https://app.test/retorno",
        cancel_url="https://app.test/planos",
        metadata={
            "price_cents": 9790,
            "plan_name": "KorusFono Pro",
            "billing_interval": "monthly",
            "customer_external_id": "cus_existing",
            "customer_document": "11222333000181",
            "existing_external_subscription_id": "sub_cpf_pending",
            "existing_external_checkout_id": "pay_cpf_pending",
            "replace_existing_checkout": True,
        },
    )

    assert session["status"] == "completed"
    assert session["external_checkout_id"] == "pay_cpf_pending"
    assert not any(method == "DELETE" for method, _url in calls)
    assert not any(
        method == "POST" and url.endswith("/subscriptions") for method, url in calls
    )


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
        billing_document="24971563792",
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
    assert session["has_billing_document"] is True
    assert session["has_cpf"] is True


@pytest.mark.asyncio
async def test_get_session_recognizes_cnpj_as_billing_document(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="invoice-cnpj@test.com",
        password_hash="hash",
        name="CNPJ Invoice User",
        billing_cnpj="11222333000181",
        billing_document_type="cnpj",
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
        external_checkout_id="stub_pay_cnpj",
        billing_document="11222333000181",
    )
    db_session.add(sub)
    await db_session.commit()

    session = await BillingCheckoutService(db_session).get_session(
        session_id="stub_pay_cnpj", professional=professional
    )

    assert session["has_billing_document"] is True
    assert session["has_cpf"] is False
    assert session["billing_document_type"] == "cnpj"


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
