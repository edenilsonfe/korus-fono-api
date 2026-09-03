"""Billing webhook and reconciliation tests."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.billing.errors import PaymentGatewayError
from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import AsaasWebhookNormalizer, StubWebhookNormalizer
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.schemas.billing import CreditCardPaymentRequest
from app.services.billing_checkout_service import BillingCheckoutService
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS
from app.services.saas_billing_service import (
    SaasBillingService,
    purchase_deduplication_id,
)


@pytest.mark.asyncio
async def test_checkout_lock_does_not_target_nullable_plan_join_on_postgresql():
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("statement captured"))
    )

    with pytest.raises(RuntimeError, match="statement captured"):
        await BillingCheckoutService(db)._get_subscription(
            session_id="checkout-session",
            professional_id="00000000-0000-0000-0000-000000000001",
            lock=True,
        )

    statement = db.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    has_nullable_join = "LEFT OUTER JOIN" in sql
    locks_only_subscriptions = sql.rstrip().endswith(
        "FOR UPDATE OF subscriptions"
    )

    assert not has_nullable_join or locks_only_subscriptions


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
async def test_annual_checkout_does_not_create_incomplete_asaas_customer(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    db_session.add(plan)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(
        return_value={"external_customer_id": "cus_incomplete"}
    )
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "/planos/pagamento?sessionId=chk_yearly",
            "session_id": "chk_yearly",
            "external_checkout_id": "chk_yearly",
            "external_subscription_id": None,
            "status": "pending",
        }
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={"planSlug": plan.slug},
    )

    assert response.status_code == 200
    gateway.create_customer.assert_not_awaited()
    metadata = gateway.create_checkout_session.await_args.kwargs["metadata"]
    assert "customer_external_id" not in metadata


@pytest.mark.asyncio
async def test_annual_checkout_prefills_complete_asaas_customer(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    professional.billing_address = "Rua das Flores"
    professional.billing_address_number = "123"
    professional.billing_address_complement = "Sala 4"
    professional.billing_province = "Centro"
    professional.billing_postal_code = "01310100"
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    db_session.add(plan)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(return_value={"external_customer_id": "cus_complete"})
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "/planos/pagamento?sessionId=chk_yearly_complete",
            "session_id": "chk_yearly_complete",
            "external_checkout_id": "chk_yearly_complete",
            "external_subscription_id": None,
            "status": "pending",
        }
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={"planSlug": plan.slug},
    )

    assert response.status_code == 200
    customer_metadata = gateway.create_customer.await_args.kwargs["metadata"]
    assert customer_metadata["customer_document"] == "24971563792"
    assert customer_metadata["customer_phone"] == "11999990000"
    assert customer_metadata["customer_address"] == "Rua das Flores"
    assert customer_metadata["customer_address_number"] == "123"
    assert customer_metadata["customer_complement"] == "Sala 4"
    assert customer_metadata["customer_province"] == "Centro"
    assert customer_metadata["customer_postal_code"] == "01310100"
    checkout_metadata = gateway.create_checkout_session.await_args.kwargs["metadata"]
    assert checkout_metadata["customer_external_id"] == "cus_complete"
    assert checkout_metadata["customer_profile_synced"] is True


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
async def test_checkout_marks_pending_charge_for_replacement_when_plan_changes(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    monthly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    yearly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    db_session.add_all([monthly_plan, yearly_plan])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=monthly_plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id="sub_monthly_pending",
        external_checkout_id="pay_monthly_pending",
        billing_document="24971563792",
    )
    db_session.add(subscription)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(return_value={"external_customer_id": "cus_existing"})
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "/planos/pagamento?sessionId=checkout-yearly-new",
            "session_id": "checkout-yearly-new",
            "external_checkout_id": "checkout-yearly-new",
            "status": "pending",
        }
    )
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": yearly_plan.slug,
            "billingDocumentType": "cpf",
            "cpf": "24971563792",
        },
    )

    assert response.status_code == 200
    metadata = gateway.create_checkout_session.await_args.kwargs["metadata"]
    assert metadata["replace_existing_checkout"] is True
    assert metadata["existing_plan_slug"] == monthly_plan.slug
    assert metadata["existing_billing_interval"] == "monthly"
    await db_session.refresh(subscription)
    assert subscription.plan_id == yearly_plan.id


@pytest.mark.asyncio
async def test_checkout_keeps_original_plan_when_previous_charge_was_paid_during_switch(
    db_session,
    professional,
    auth_headers,
    api_client,
    monkeypatch,
):
    professional.cpf = "24971563792"
    monthly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    yearly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    db_session.add_all([monthly_plan, yearly_plan])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=monthly_plan.id,
        status="incomplete",
        provider="asaas",
        external_subscription_id="sub_monthly_paid",
        external_checkout_id="pay_monthly_paid",
        checkout_charge_cents=monthly_plan.price_cents,
        billing_document="24971563792",
    )
    db_session.add(subscription)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer = AsyncMock(return_value={"external_customer_id": "cus_existing"})
    gateway.create_checkout_session = AsyncMock(
        return_value={
            "checkout_url": "/planos/pagamento?sessionId=pay_monthly_paid",
            "session_id": "pay_monthly_paid",
            "external_subscription_id": "sub_monthly_paid",
            "external_checkout_id": "pay_monthly_paid",
            "status": "completed",
            "preserve_existing_plan": True,
        }
    )
    reconciliation = AsyncMock()
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    monkeypatch.setattr(
        "app.api.v1.billing.BillingReconciliationService",
        lambda _db: reconciliation,
    )

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": yearly_plan.slug,
            "billingDocumentType": "cpf",
            "cpf": "24971563792",
        },
    )

    assert response.status_code == 200
    await db_session.refresh(subscription)
    assert subscription.plan_id == monthly_plan.id
    assert subscription.checkout_charge_cents == monthly_plan.price_cents
    reconciliation.reconcile_professional.assert_awaited_once_with(professional.id)


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
        if method == "PUT" and url.endswith("/subscriptions/sub_cnpj_new"):
            assert kwargs["json_body"] == {"status": "INACTIVE"}
            return {"id": "sub_cnpj_new", "status": "INACTIVE"}
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
async def test_asaas_cancels_pending_annual_checkout_before_switching_to_monthly(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET" and url.endswith("/checkouts/chk_annual_pending"):
            return {
                "id": "chk_annual_pending",
                "status": "ACTIVE",
                "externalReference": "account-1:korusfono_pro_yearly",
            }
        if method == "POST" and url.endswith("/checkouts/chk_annual_pending/cancel"):
            return {"id": "chk_annual_pending", "status": "CANCELED"}
        if method == "POST" and url.endswith("/subscriptions"):
            return {"id": "sub_monthly_new"}
        if method == "GET" and url.endswith("/subscriptions/sub_monthly_new/payments"):
            return {
                "data": [
                    {
                        "id": "pay_monthly_new",
                        "status": "PENDING",
                        "invoiceUrl": "https://sandbox.asaas.com/i/pay_monthly_new",
                    }
                ]
            }
        if method == "POST" and url.endswith("/payments/pay_monthly_new"):
            return {"id": "pay_monthly_new"}
        if method == "PUT" and url.endswith("/subscriptions/sub_monthly_new"):
            assert kwargs["json_body"] == {"status": "INACTIVE"}
            return {"id": "sub_monthly_new", "status": "INACTIVE"}
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
            "customer_document": "24971563792",
            "customer_document_synced": True,
            "existing_external_checkout_id": "chk_annual_pending",
            "existing_plan_slug": "korusfono_pro_yearly",
            "existing_billing_interval": "yearly",
            "replace_existing_checkout": True,
        },
    )

    assert session["external_subscription_id"] == "sub_monthly_new"
    assert (
        "POST",
        "https://api-sandbox.asaas.com/v3/checkouts/chk_annual_pending/cancel",
    ) in calls


@pytest.mark.asyncio
async def test_asaas_preserves_paid_annual_checkout_during_plan_switch(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    calls: list[tuple[str, str]] = []

    async def fake_request_json(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET" and url.endswith("/checkouts/chk_annual_paid"):
            return {
                "id": "chk_annual_paid",
                "status": "PAID",
                "externalReference": "account-1:korusfono_pro_yearly",
            }
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
            "existing_external_checkout_id": "chk_annual_paid",
            "existing_plan_slug": "korusfono_pro_yearly",
            "existing_billing_interval": "yearly",
            "replace_existing_checkout": True,
        },
    )

    assert session["status"] == "completed"
    assert session["preserve_existing_plan"] is True
    assert not any(method == "POST" for method, _url in calls)


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
async def test_asaas_yearly_checkout_is_single_purchase_with_up_to_ten_installments(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "POST"
        assert url == "https://api-sandbox.asaas.com/v3/checkouts"
        captured.update(kwargs["json_body"])
        return {
            "id": "chk_yearly_10x",
            "link": "https://sandbox.asaas.com/checkoutSession/show?id=chk_yearly_10x",
            "status": "ACTIVE",
        }

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    session = await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="korusfono_pro_yearly",
        success_url="https://app.test/planos/retorno?status=pending",
        cancel_url="https://app.test/planos?checkout=cancel",
        metadata={
            "price_cents": 97000,
            "charge_cents": 93000,
            "plan_name": "KorusFono Pro",
            "billing_interval": "yearly",
        },
    )

    assert captured["billingTypes"] == ["PIX", "CREDIT_CARD"]
    assert captured["chargeTypes"] == ["DETACHED", "INSTALLMENT"]
    assert captured["installment"] == {"maxInstallmentCount": 10}
    assert "customer" not in captured
    assert captured["externalReference"] == "account-1:korusfono_pro_yearly"
    assert captured["items"] == [
        {
            "name": "KorusFono Pro",
            "description": "Acesso ao KorusFono por 12 meses",
            "quantity": 1,
            "value": 930.0,
        }
    ]
    assert captured["callback"] == {
        "successUrl": "https://app.test/planos/retorno?status=pending",
        "cancelUrl": "https://app.test/planos?checkout=cancel",
        "expiredUrl": "https://app.test/planos?checkout=cancel",
    }
    assert session["external_subscription_id"] is None
    assert session["external_checkout_id"] == "chk_yearly_10x"
    assert session["session_id"] == "chk_yearly_10x"
    assert session["invoice_url"].endswith("id=chk_yearly_10x")
    assert session["status"] == "pending"


@pytest.mark.asyncio
async def test_asaas_yearly_checkout_uses_prefilled_customer_when_profile_is_complete(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"id": "chk_prefilled", "status": "ACTIVE"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="korusfono_pro_yearly",
        success_url="https://app.test/retorno",
        cancel_url="https://app.test/planos",
        metadata={
            "price_cents": 97000,
            "plan_name": "KorusFono Pro",
            "billing_interval": "yearly",
            "customer_external_id": "cus_complete",
            "customer_profile_synced": True,
        },
    )

    assert captured["customer"] == "cus_complete"


@pytest.mark.asyncio
async def test_asaas_updates_existing_customer_with_complete_billing_profile(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "PUT"
        assert url.endswith("/customers/cus_existing")
        captured.update(kwargs["json_body"])
        return {"id": "cus_existing"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    await gateway.update_customer_profile(
        customer_id="cus_existing",
        metadata={
            "customer_document": "249.715.637-92",
            "customer_phone": "(11) 99999-0000",
            "customer_address": "Rua das Flores",
            "customer_address_number": "123",
            "customer_complement": "Sala 4",
            "customer_province": "Centro",
            "customer_postal_code": "01310-100",
        },
    )

    assert captured == {
        "cpfCnpj": "24971563792",
        "phone": "11999990000",
        "address": "Rua das Flores",
        "addressNumber": "123",
        "complement": "Sala 4",
        "province": "Centro",
        "postalCode": "01310100",
    }


@pytest.mark.asyncio
async def test_asaas_yearly_checkout_builds_sandbox_link_when_response_has_only_id(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"

    async def fake_request_json(method, url, **kwargs):
        return {"id": "chk_link_fallback", "status": "ACTIVE"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    session = await gateway.create_checkout_session(
        account_id="account-1",
        plan_slug="korusfono_pro_yearly",
        success_url="https://app.test/success",
        cancel_url="https://app.test/cancel",
        metadata={
            "price_cents": 97000,
            "billing_interval": "yearly",
            "customer_external_id": "cus_existing",
        },
    )

    assert session["invoice_url"] == (
        "https://sandbox.asaas.com/checkoutSession/show?id=chk_link_fallback"
    )


@pytest.mark.asyncio
async def test_stub_webhook_activates_subscription(db_session, monkeypatch):
    meta_purchase = AsyncMock(return_value=True)
    posthog_purchase = AsyncMock(return_value=True)
    verification_send = MagicMock()
    monkeypatch.setattr(
        "app.services.saas_billing_service.MetaPixelService.track_purchase",
        meta_purchase,
    )
    monkeypatch.setattr(
        "app.services.saas_billing_service.PostHogAnalyticsService.track_purchase",
        posthog_purchase,
    )
    monkeypatch.setattr(
        "app.services.saas_billing_service.send_email_verification_email_sync",
        verification_send,
    )
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="billing@test.com",
        password_hash="hash",
        name="Billing User",
        subscription_status="trialing",
        signup_payment_required=True,
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
        checkout_session_id=professional.id,
        checkout_charge_cents=9300,
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
    assert professional.signup_payment_required is False
    assert sub.status == "active"
    verification_send.assert_called_once()
    assert verification_send.call_args.args[:2] == (professional.email, professional.name)
    posthog_purchase.assert_awaited_once_with(
        professional_id=professional_id,
        plan_slug=plan.slug,
        value_cents=9300,
        currency=plan.currency,
        billing_event_id="stub-evt-1",
        session_id=str(professional.id),
    )


@pytest.mark.asyncio
async def test_subscription_created_does_not_unlock_paid_signup(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="pending-paid-signup@test.com",
        password_hash="hash",
        name="Pending Paid Signup",
        subscription_status="trialing",
        signup_payment_required=True,
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    sub = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="stub",
        external_subscription_id="stub_sub_created_only",
    )
    db_session.add(sub)
    await db_session.commit()

    events = StubWebhookNormalizer().normalize(
        {
            "id": "evt-subscription-created-only",
            "event_type": InternalBillingEventType.SUBSCRIPTION_CREATED.value,
            "professional_id": str(professional.id),
            "plan_slug": plan.slug,
            "provider": "stub",
            "external_subscription_id": sub.external_subscription_id,
            "subscription_status": "active",
        },
        {},
    )
    await SaasBillingService(db_session).apply_normalized_events(events)

    await db_session.refresh(professional)
    assert professional.signup_payment_required is True


def test_asaas_payment_webhook_preserves_checkout_session_id():
    events = AsaasWebhookNormalizer().normalize(
        {
            "event": "PAYMENT_CONFIRMED",
            "payment": {
                "id": "pay_installment_1",
                "checkoutSession": "chk_annual_12x",
                "externalReference": "account-1:korusfono_pro_yearly",
                "status": "CONFIRMED",
            },
        },
        {},
    )

    assert len(events) == 1
    assert events[0].payload["external_checkout_id"] == "chk_annual_12x"
    assert events[0].payload["checkout_session_id"] == "chk_annual_12x"


def test_asaas_success_webhooks_share_purchase_deduplication_id():
    normalizer = AsaasWebhookNormalizer()
    payload = {
        "payment": {
            "id": "pay_same_charge",
            "externalReference": "account-1:korusfono_pro_monthly:checkout-local-1",
            "status": "CONFIRMED",
        }
    }

    confirmed = normalizer.normalize(
        {"event": "PAYMENT_CONFIRMED", **payload},
        {},
    )[0]
    received = normalizer.normalize(
        {"event": "PAYMENT_RECEIVED", **payload},
        {},
    )[0]

    assert confirmed.external_event_id != received.external_event_id
    assert purchase_deduplication_id(confirmed) == "asaas-pay_same_charge"
    assert purchase_deduplication_id(received) == "asaas-pay_same_charge"


@pytest.mark.asyncio
async def test_asaas_checkout_webhook_without_external_reference_activates_annual_plan(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-webhook@test.com",
        password_hash="hash",
        name="Annual Webhook",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=plan.id,
        status="incomplete",
        provider="asaas",
        external_checkout_id="chk_without_reference",
    )
    db_session.add(subscription)
    await db_session.commit()

    events = AsaasWebhookNormalizer().normalize(
        {
            "event": "PAYMENT_CONFIRMED",
            "payment": {
                "id": "pay_without_reference",
                "checkoutSession": "chk_without_reference",
                "paymentDate": "2026-08-18",
                "status": "CONFIRMED",
            },
        },
        {},
    )
    await SaasBillingService(db_session).apply_normalized_events(events)

    await db_session.refresh(professional)
    await db_session.refresh(subscription)
    assert professional.subscription_status == "active"
    assert subscription.status == "active"
    assert subscription.current_period_end == datetime(2027, 8, 18)


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
async def test_get_session_annual_uses_hosted_checkout_instead_of_payment(db_session):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-session@test.com",
        password_hash="hash",
        name="Annual Session User",
        cpf="24971563792",
        subscription_status="trialing",
        trial_started_at=datetime.now(UTC),
        trial_ends_at=datetime.now(UTC),
    )
    db_session.add_all([plan, professional])
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            status="incomplete",
            provider="asaas",
            external_subscription_id=None,
            external_checkout_id="chk_annual_session",
            billing_document="24971563792",
        )
    )
    await db_session.commit()

    invoice = "https://sandbox.asaas.com/checkoutSession/show?id=chk_annual_session"
    gateway = AsyncMock()
    gateway.get_checkout = AsyncMock(
        return_value={"id": "chk_annual_session", "status": "ACTIVE", "link": invoice}
    )

    with patch(
        "app.services.billing_checkout_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        session = await BillingCheckoutService(db_session).get_session(
            session_id="chk_annual_session", professional=professional
        )

    assert session["invoice_url"] == invoice
    assert session["status"] == "pending"
    gateway.get_checkout.assert_awaited_once_with("chk_annual_session")
    gateway.get_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_credit_card_checkout_processes_authenticated_request_without_echoing_pan(
    api_client,
    auth_headers,
    monkeypatch,
):
    service = AsyncMock()
    service.pay_credit_card = AsyncMock(
        return_value={
            "session_id": "checkout-local-1",
            "provider": "asaas",
            "status": "pending",
            "message": "Pagamento em processamento.",
        }
    )
    monkeypatch.setattr(
        "app.api.v1.billing.BillingCheckoutService",
        lambda _db: service,
    )
    monkeypatch.setattr(
        "app.api.v1.billing.enforce_card_payment_rate_limit",
        lambda **_kwargs: None,
    )

    response = await api_client.post(
        "/api/v1/billing/checkout/checkout-local-1/credit-card",
        headers={**auth_headers, "x-forwarded-for": "203.0.113.10, 198.51.100.2"},
        json={
            "holderName": "Maria da Silva",
            "number": "4111 1111 1111 1111",
            "expiryMonth": "05",
            "expiryYear": "2030",
            "ccv": "123",
            "holderEmail": "maria@example.com",
            "holderDocument": "249.715.637-92",
            "postalCode": "01310-100",
            "addressNumber": "100",
            "addressComplement": "Sala 2",
            "phone": "(11) 99999-0000",
            "installments": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "sessionId": "checkout-local-1",
        "provider": "asaas",
        "status": "pending",
        "message": "Pagamento em processamento.",
    }
    assert "4111111111111111" not in response.text
    assert "123" not in response.text
    request_payload = service.pay_credit_card.await_args.kwargs["payload"]
    assert request_payload.number.get_secret_value() == "4111111111111111"
    assert request_payload.ccv.get_secret_value() == "123"
    assert service.pay_credit_card.await_args.kwargs["remote_ip"] == "203.0.113.10"


@pytest.mark.asyncio
async def test_credit_card_checkout_rejects_invalid_pan_before_gateway(
    api_client,
    auth_headers,
    monkeypatch,
):
    service = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.billing.BillingCheckoutService",
        lambda _db: service,
    )

    response = await api_client.post(
        "/api/v1/billing/checkout/checkout-local-1/credit-card",
        headers=auth_headers,
        json={
            "holderName": "Maria da Silva",
            "number": "4111111111111112",
            "expiryMonth": "05",
            "expiryYear": "2030",
            "ccv": "123",
            "holderEmail": "maria@example.com",
            "holderDocument": "24971563792",
            "postalCode": "01310100",
            "addressNumber": "100",
            "phone": "11999990000",
            "installments": 1,
        },
    )

    assert response.status_code == 422
    service.pay_credit_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_asaas_creates_monthly_subscription_with_card_and_immediate_first_charge(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        if method == "POST" and url.endswith("/subscriptions"):
            captured.update(kwargs["json_body"])
            assert kwargs["timeout"] == 60.0
            return {"id": "sub_card_monthly", "status": "ACTIVE"}
        if method == "GET" and url.endswith("/subscriptions/sub_card_monthly/payments"):
            return {
                "data": [
                    {
                        "id": "pay_card_monthly",
                        "status": "CONFIRMED",
                        "externalReference": "account-1:korusfono_pro_monthly:checkout-1",
                    }
                ]
            }
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
            "addressComplement": None,
            "phone": "11999990000",
            "mobilePhone": "11999990000",
        },
        remote_ip="203.0.113.10",
    )

    assert captured["billingType"] == "CREDIT_CARD"
    assert captured["nextDueDate"] == date.today().isoformat()
    assert captured["value"] == 97.9
    assert captured["cycle"] == "MONTHLY"
    assert captured["externalReference"] == (
        "account-1:korusfono_pro_monthly:checkout-1"
    )
    assert captured["creditCard"]["number"] == "4111111111111111"
    assert captured["remoteIp"] == "203.0.113.10"
    assert result["external_subscription_id"] == "sub_card_monthly"
    assert result["payment"]["id"] == "pay_card_monthly"


@pytest.mark.asyncio
async def test_asaas_creates_annual_card_payment_with_selected_installments(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "POST"
        assert url.endswith("/payments")
        assert kwargs["timeout"] == 60.0
        captured.update(kwargs["json_body"])
        return {
            "id": "pay_card_annual",
            "installment": "ins_card_annual",
            "status": "CONFIRMED",
        }

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    result = await gateway.create_credit_card_payment(
        customer_id="cus_card",
        account_id="account-1",
        plan_slug="korusfono_pro_yearly",
        description="KorusFono Pro — 12 meses",
        value_cents=97000,
        installments=10,
        checkout_reference="checkout-annual-1",
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

    assert captured["billingType"] == "CREDIT_CARD"
    assert captured["installmentCount"] == 10
    assert captured["totalValue"] == 970.0
    assert "value" not in captured
    assert captured["externalReference"] == (
        "account-1:korusfono_pro_yearly:checkout-annual-1"
    )
    assert result["payment_id"] == "pay_card_annual"


@pytest.mark.asyncio
async def test_asaas_creates_annual_pix_payment_for_in_app_qr_code(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "POST"
        assert url.endswith("/payments")
        captured.update(kwargs["json_body"])
        return {"id": "pay_pix_annual", "status": "PENDING"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    result = await gateway.create_pix_payment(
        customer_id="cus_pix",
        account_id="account-1",
        plan_slug="korusfono_pro_yearly",
        description="KorusFono Pro — acesso por 12 meses",
        value_cents=93000,
        checkout_reference="checkout-annual-pix-1",
    )

    assert captured == {
        "customer": "cus_pix",
        "billingType": "PIX",
        "dueDate": date.today().isoformat(),
        "value": 930.0,
        "description": "KorusFono Pro — acesso por 12 meses",
        "externalReference": (
            "account-1:korusfono_pro_yearly:checkout-annual-pix-1"
        ),
    }
    assert result == {
        "payment_id": "pay_pix_annual",
        "payment": {"id": "pay_pix_annual", "status": "PENDING"},
    }


@pytest.mark.asyncio
async def test_asaas_single_payment_never_uses_undefined_billing_type(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-key"
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    captured: dict = {}

    async def fake_request_json(method, url, **kwargs):
        assert method == "POST"
        assert url.endswith("/payments")
        captured.update(kwargs["json_body"])
        return {"id": "pay_single", "status": "PENDING"}

    monkeypatch.setattr("app.billing.asaas_gateway.request_json", fake_request_json)

    await gateway.create_single_payment(
        customer_id="cus_single",
        value_cents=9790,
        description="KorusFono Pro",
        external_reference="account-1:korusfono_pro_monthly",
    )

    assert captured["billingType"] == "PIX"


def _valid_credit_card_payload(*, installments: int = 1) -> CreditCardPaymentRequest:
    return CreditCardPaymentRequest.model_validate(
        {
            "holderName": "Maria da Silva",
            "number": "4111111111111111",
            "expiryMonth": "05",
            "expiryYear": "2030",
            "ccv": "123",
            "holderEmail": "maria@example.com",
            "holderDocument": "24971563792",
            "postalCode": "01310100",
            "addressNumber": "100",
            "phone": "11999990000",
            "installments": installments,
        }
    )


def test_credit_card_schema_masks_pan_and_cvv_in_repr():
    payload = _valid_credit_card_payload()

    rendered = repr(payload)

    assert "4111111111111111" not in rendered
    assert "ccv=SecretStr('**********')" in rendered
    assert "number=SecretStr('**********')" in rendered


def test_credit_card_schema_rejects_more_than_ten_installments():
    with pytest.raises(ValidationError):
        _valid_credit_card_payload(installments=11)


@pytest.mark.asyncio
async def test_annual_transparent_card_replaces_hosted_checkout_and_keeps_discounted_charge(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="transparent-annual@test.com",
        password_hash="hash",
        name="Transparent Annual",
        phone="11999990000",
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
        checkout_session_id=professional.id,
        checkout_charge_cents=93000,
        external_checkout_id="chk_hosted_old",
        billing_document="24971563792",
    )
    db_session.add(sub)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_payments_by_external_reference = AsyncMock(return_value=[])
    gateway.create_credit_card_payment = AsyncMock(
        return_value={
            "payment_id": "pay_transparent_annual",
            "payment": {"id": "pay_transparent_annual", "status": "AWAITING_RISK_ANALYSIS"},
        }
    )
    gateway.cancel_checkout = AsyncMock(return_value=None)
    customer_service = AsyncMock()
    customer_service.ensure_customer = AsyncMock(return_value="cus_transparent")

    with (
        patch(
            "app.services.billing_checkout_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        patch(
            "app.services.billing_checkout_service.BillingCustomerService",
            return_value=customer_service,
        ),
    ):
        result = await BillingCheckoutService(db_session).pay_credit_card(
            session_id=str(professional.id),
            professional=professional,
            payload=_valid_credit_card_payload(installments=10),
            remote_ip="203.0.113.10",
        )

    assert result["status"] == "pending"
    assert sub.external_checkout_id == "pay_transparent_annual"
    gateway.create_credit_card_payment.assert_awaited_once()
    create_kwargs = gateway.create_credit_card_payment.await_args.kwargs
    assert create_kwargs["value_cents"] == 93000
    assert create_kwargs["installments"] == 10
    assert create_kwargs["remote_ip"] == "203.0.113.10"
    gateway.cancel_checkout.assert_awaited_once_with("chk_hosted_old")


@pytest.mark.asyncio
async def test_annual_pix_is_generated_inside_checkout_and_replaces_hosted_checkout(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="transparent-annual-pix@test.com",
        password_hash="hash",
        name="Transparent Annual PIX",
        phone="11999990000",
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
        checkout_session_id=professional.id,
        checkout_charge_cents=93000,
        external_checkout_id="chk_hosted_annual_pix",
        billing_document="24971563792",
    )
    db_session.add(sub)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_payments_by_external_reference = AsyncMock(return_value=[])
    gateway.create_pix_payment = AsyncMock(
        return_value={
            "payment_id": "pay_transparent_annual_pix",
            "payment": {"id": "pay_transparent_annual_pix", "status": "PENDING"},
        }
    )
    gateway.get_pix_qr_code = AsyncMock(
        return_value={
            "encoded_image": "base64-qr-code",
            "payload": "000201-pix-copia-e-cola",
            "expiration_date": "2026-08-28T03:00:00Z",
        }
    )
    gateway.cancel_checkout = AsyncMock(return_value=None)
    customer_service = AsyncMock()
    customer_service.ensure_customer = AsyncMock(return_value="cus_transparent_pix")

    with (
        patch(
            "app.services.billing_checkout_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        patch(
            "app.services.billing_checkout_service.BillingCustomerService",
            return_value=customer_service,
        ),
    ):
        result = await BillingCheckoutService(db_session).generate_pix(
            session_id=str(professional.id),
            professional=professional,
        )

    assert result == {
        "session_id": str(professional.id),
        "provider": "asaas",
        "encoded_image": "base64-qr-code",
        "payload": "000201-pix-copia-e-cola",
        "expiration_date": "2026-08-28T03:00:00Z",
    }
    assert sub.external_checkout_id == "pay_transparent_annual_pix"
    assert sub.payment_method == "pix"
    gateway.create_pix_payment.assert_awaited_once_with(
        customer_id="cus_transparent_pix",
        account_id=str(professional.id),
        plan_slug="korusfono_pro_yearly",
        description="KorusFono Pro — acesso por 12 meses",
        value_cents=93000,
        checkout_reference=str(professional.id),
    )
    gateway.get_pix_qr_code.assert_awaited_once_with("pay_transparent_annual_pix")
    gateway.cancel_checkout.assert_awaited_once_with("chk_hosted_annual_pix")


@pytest.mark.asyncio
async def test_annual_card_replaces_pending_pix_payment_instead_of_reusing_it(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-card-after-pix@test.com",
        password_hash="hash",
        name="Annual Card After PIX",
        phone="11999990000",
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
        checkout_session_id=professional.id,
        checkout_charge_cents=97000,
        external_checkout_id="pay_pix_annual_old",
        billing_document="24971563792",
    )
    db_session.add(sub)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_payments_by_external_reference = AsyncMock(
        return_value=[
            {
                "id": "pay_pix_annual_old",
                "status": "PENDING",
                "billingType": "PIX",
            }
        ]
    )
    gateway.create_credit_card_payment = AsyncMock(
        return_value={
            "payment_id": "pay_card_annual_new",
            "payment": {"id": "pay_card_annual_new", "status": "PENDING"},
        }
    )
    gateway.delete_payment = AsyncMock(return_value=None)
    gateway.cancel_checkout = AsyncMock(return_value=None)
    customer_service = AsyncMock()
    customer_service.ensure_customer = AsyncMock(return_value="cus_card_after_pix")

    with (
        patch(
            "app.services.billing_checkout_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        patch(
            "app.services.billing_checkout_service.BillingCustomerService",
            return_value=customer_service,
        ),
    ):
        result = await BillingCheckoutService(db_session).pay_credit_card(
            session_id=str(professional.id),
            professional=professional,
            payload=_valid_credit_card_payload(installments=10),
            remote_ip="203.0.113.10",
        )

    assert result["status"] == "pending"
    assert sub.external_checkout_id == "pay_card_annual_new"
    gateway.create_credit_card_payment.assert_awaited_once()
    gateway.delete_payment.assert_awaited_once_with("pay_pix_annual_old")
    gateway.cancel_checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_annual_pix_replaces_pending_card_payment_without_hosted_checkout(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="annual-pix-after-card@test.com",
        password_hash="hash",
        name="Annual PIX After Card",
        phone="11999990000",
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
        checkout_session_id=professional.id,
        checkout_charge_cents=97000,
        external_checkout_id="pay_card_annual_old",
        billing_document="24971563792",
    )
    db_session.add(sub)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_payments_by_external_reference = AsyncMock(
        return_value=[
            {
                "id": "pay_card_annual_old",
                "status": "PENDING",
                "billingType": "CREDIT_CARD",
            }
        ]
    )
    gateway.create_pix_payment = AsyncMock(
        return_value={
            "payment_id": "pay_pix_annual_new",
            "payment": {"id": "pay_pix_annual_new", "status": "PENDING"},
        }
    )
    gateway.get_pix_qr_code = AsyncMock(
        return_value={
            "encoded_image": "base64-new-pix",
            "payload": "000201-new-pix",
            "expiration_date": "2026-08-28T03:00:00Z",
        }
    )
    gateway.delete_payment = AsyncMock(return_value=None)
    gateway.cancel_checkout = AsyncMock(return_value=None)
    customer_service = AsyncMock()
    customer_service.ensure_customer = AsyncMock(return_value="cus_pix_after_card")

    with (
        patch(
            "app.services.billing_checkout_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        patch(
            "app.services.billing_checkout_service.BillingCustomerService",
            return_value=customer_service,
        ),
    ):
        result = await BillingCheckoutService(db_session).generate_pix(
            session_id=str(professional.id),
            professional=professional,
        )

    assert result["payload"] == "000201-new-pix"
    assert sub.external_checkout_id == "pay_pix_annual_new"
    gateway.create_pix_payment.assert_awaited_once()
    gateway.delete_payment.assert_awaited_once_with("pay_card_annual_old")
    gateway.cancel_checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_monthly_transparent_card_creates_recurring_subscription_and_cancels_old_one(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="transparent-monthly@test.com",
        password_hash="hash",
        name="Transparent Monthly",
        phone="11999990000",
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
        checkout_session_id=professional.id,
        checkout_charge_cents=9790,
        external_subscription_id="sub_hosted_old",
        external_checkout_id="pay_hosted_old",
        billing_document="24971563792",
    )
    db_session.add(sub)
    await db_session.commit()

    gateway = AsyncMock()
    gateway.list_subscriptions_by_external_reference = AsyncMock(return_value=[])
    gateway.create_credit_card_subscription = AsyncMock(
        return_value={
            "external_subscription_id": "sub_transparent_new",
            "payment": {"id": "pay_transparent_new", "status": "AWAITING_RISK_ANALYSIS"},
        }
    )
    gateway.cancel_subscription = AsyncMock(return_value={"status": "canceled"})
    customer_service = AsyncMock()
    customer_service.ensure_customer = AsyncMock(return_value="cus_transparent")

    with (
        patch(
            "app.services.billing_checkout_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        patch(
            "app.services.billing_checkout_service.BillingCustomerService",
            return_value=customer_service,
        ),
    ):
        result = await BillingCheckoutService(db_session).pay_credit_card(
            session_id=str(professional.id),
            professional=professional,
            payload=_valid_credit_card_payload(),
            remote_ip="203.0.113.10",
        )

    assert result["status"] == "pending"
    assert sub.external_subscription_id == "sub_transparent_new"
    assert sub.external_checkout_id == "pay_transparent_new"
    assert sub.payment_method == "credit_card"
    gateway.cancel_subscription.assert_awaited_once_with(
        external_subscription_id="sub_hosted_old"
    )


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
    assert sub.payment_method == "credit_card"
    gateway.ensure_card_billing.assert_awaited_once_with("pay_prepare_card")


@pytest.mark.asyncio
async def test_prepare_annual_checkout_returns_hosted_link_without_locking_payment_method(
    db_session,
):
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="prepare-annual@test.com",
        password_hash="hash",
        name="Prepare Annual User",
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
        external_subscription_id=None,
        external_checkout_id="chk_prepare_annual",
    )
    db_session.add(sub)
    await db_session.commit()

    invoice = "https://sandbox.asaas.com/checkoutSession/show?id=chk_prepare_annual"
    gateway = AsyncMock()
    gateway._hosted_checkout_url = MagicMock(return_value=invoice)

    with patch(
        "app.services.billing_checkout_service.AsaasPaymentGateway",
        return_value=gateway,
    ):
        result = await BillingCheckoutService(db_session).prepare_card_invoice(
            session_id="chk_prepare_annual", professional=professional
        )

    assert result["invoice_url"] == invoice
    assert sub.payment_method is None
    gateway.get_checkout.assert_not_awaited()
    gateway._hosted_checkout_url.assert_called_once_with({"id": "chk_prepare_annual"})
    gateway.ensure_card_billing.assert_not_awaited()
