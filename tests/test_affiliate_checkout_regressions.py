"""Affiliate review regressions with provider boundaries mocked."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.models.affiliate import (
    AffiliateCreditCheckout,
    AffiliatePolicy,
    AffiliateReferral,
)
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.affiliate_credit_service import AffiliateCreditService
from app.services.affiliate_service import AffiliateForbiddenError, AffiliateService

# Explicit re-export makes the shared pytest fixture discoverable in this module.
from tests.test_affiliate_credit import (
    credit_checkout as credit_checkout,  # noqa: PLC0414
)

pytestmark = pytest.mark.asyncio


async def test_partner_cannot_refer_own_new_clinical_account(db_session):
    service = AffiliateService(db_session)
    db_session.add(
        AffiliatePolicy(
            mode="partner",
            version=1,
            status="active",
            terms_version="review-v1",
            commission_bps=2000,
            effective_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    partner = await service.invite_partner(
        email="self-review@example.com", public_name="Partner"
    )
    activation = await service.activate_partner(
        participant=partner, terms_version="review-v1"
    )
    own_account = Professional(
        email=partner.email, name="Partner", password_hash="unused"
    )
    db_session.add(own_account)
    await db_session.flush()
    with pytest.raises(AffiliateForbiddenError):
        await service.register_referral(
            code=activation.code,
            referred_professional=own_account,
            request_ip="203.0.113.7",
            user_agent="review",
        )


async def test_next_overdue_cycle_can_checkout_after_previous_credit_settled(
    api_client, db_session, professional, auth_headers, credit_checkout
):
    plan, _participant, gateway = credit_checkout
    first = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert first.status_code == 200
    sub = await db_session.scalar(select(Subscription))
    old_session_id = sub.checkout_session_id
    await AffiliateCreditService(db_session).settle_checkout_reservation(
        reservation_id=str(sub.checkout_session_id), payment_id="pay-credit"
    )
    sub.status = "past_due"
    professional.subscription_status = "past_due"
    gateway.list_subscription_payments.return_value = [
        {"id": "pay-credit", "subscription": "sub-credit", "status": "RECEIVED"},
        {
            "id": "pay-next",
            "subscription": "sub-credit",
            "status": "OVERDUE",
            "dueDate": "2026-10-07",
        },
    ]
    gateway.create_checkout_session.return_value = {
        "external_subscription_id": "sub-credit",
        "external_checkout_id": "pay-next",
        "session_id": "pay-next",
        "status": "pending",
    }
    await db_session.commit()
    response = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert response.status_code == 200, response.text
    assert sub.checkout_session_id != old_session_id
    assert (
        gateway.create_checkout_session.await_args.kwargs["metadata"][
            "existing_external_checkout_id"
        ]
        == "pay-next"
    )
    previous = await db_session.scalar(select(AffiliateCreditCheckout))
    assert previous.state == "settled" and previous.source_payment_id == "pay-credit"
    await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    gateway.list_subscription_payments.assert_awaited_once()


async def test_reused_annual_checkout_must_reflect_new_credit_amount():
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._get_reusable_annual_checkout = AsyncMock(
        return_value={
            "id": "old-checkout",
            "status": "PENDING",
            "value": 100,
            "link": "https://example.com/checkout",
        }
    )
    gateway.cancel_checkout = AsyncMock()
    gateway.create_hosted_annual_checkout = AsyncMock(
        return_value={
            "id": "new-checkout",
            "status": "PENDING",
            "link": "https://example.com/new",
        }
    )
    result = await gateway.create_checkout_session(
        account_id="account",
        plan_slug="annual",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        metadata={
            "billing_interval": "yearly",
            "price_cents": 10000,
            "charge_cents": 5000,
            "affiliate_credit_cents": 5000,
            "existing_external_checkout_id": "old-checkout",
        },
    )
    assert result["external_checkout_id"] != "old-checkout", (
        "Old checkout still charges 100 instead of 50"
    )
    gateway.cancel_checkout.assert_awaited_once_with("old-checkout")
    assert (
        gateway.create_hosted_annual_checkout.await_args.kwargs["value_cents"] == 5000
    )


async def test_old_self_referral_is_rejected_before_financial_reward(db_session):
    service = AffiliateService(db_session)
    policy = AffiliatePolicy(
        mode="partner",
        version=1,
        status="active",
        terms_version="v1",
        commission_bps=2000,
        effective_at=datetime.now(UTC),
    )
    db_session.add(policy)
    await db_session.flush()
    partner = await service.invite_partner(
        email="same@example.com", public_name="Partner"
    )
    activation = await service.activate_partner(participant=partner, terms_version="v1")
    from app.models.affiliate import AffiliateCode

    code = await db_session.scalar(
        select(AffiliateCode).where(AffiliateCode.code == activation.code)
    )
    buyer = Professional(email="SAME@example.com", name="Same", password_hash="unused")
    db_session.add(buyer)
    await db_session.flush()
    referral = AffiliateReferral(
        participant_id=partner.id,
        code_id=code.id,
        referred_professional_id=buyer.id,
        policy_id=policy.id,
        mode="partner",
        policy_snapshot=policy.snapshot(),
        benefit_expires_at=datetime.now(UTC),
    )
    db_session.add(referral)
    await db_session.flush()
    reward = await service.record_external_payment(
        referred_professional_id=buyer.id,
        external_payment_id="self-pay",
        external_event_id="self-event",
        provider_event="PAYMENT_RECEIVED",
        received_revenue_cents=10000,
        plan_interval="monthly",
        occurred_at=datetime.now(UTC),
    )
    assert reward is None
    assert referral.status == "rejected"
    assert (await service.balances(partner.id))["pending"] == 0


@pytest.mark.parametrize("status", ["RECEIVED", "AWAITING_RISK_ANALYSIS"])
async def test_overdue_local_state_cannot_reopen_paid_reservation(
    api_client, db_session, professional, auth_headers, credit_checkout, status
):
    plan, _participant, gateway = credit_checkout
    first = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert first.status_code == 200
    sub = await db_session.scalar(select(Subscription))
    await AffiliateCreditService(db_session).settle_checkout_reservation(
        reservation_id=str(sub.checkout_session_id)
    )
    original_id = sub.checkout_session_id
    sub.status = "past_due"
    professional.subscription_status = "past_due"
    await db_session.commit()
    gateway.list_subscription_payments.return_value = [
        {"id": "pay-other", "subscription": "sub-credit", "status": status},
        {"id": "pay-foreign", "subscription": "sub-foreign", "status": "OVERDUE"},
    ]
    response = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert response.status_code == 409
    assert sub.checkout_session_id == original_id
    assert gateway.create_checkout_session.await_count == 1


async def test_paid_annual_checkout_does_not_consume_new_credit():
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._get_reusable_annual_checkout = AsyncMock(
        return_value={
            "id": "paid-checkout",
            "status": "PAID",
            "items": [{"value": 50, "quantity": 1}],
            "link": "https://example.com/checkout",
        }
    )
    gateway.cancel_checkout = AsyncMock()
    result = await gateway.create_checkout_session(
        account_id="account",
        plan_slug="annual",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        metadata={
            "billing_interval": "yearly",
            "price_cents": 10000,
            "charge_cents": 5000,
            "affiliate_credit_cents": 5000,
            "existing_external_checkout_id": "paid-checkout",
        },
    )
    assert result["status"] == "completed"
    assert result["affiliate_credit_not_applied"] is True
    gateway.cancel_checkout.assert_not_awaited()


async def test_http_returns_unused_credit_when_existing_checkout_already_paid(
    api_client, db_session, professional, auth_headers, credit_checkout, monkeypatch
):
    plan, participant, gateway = credit_checkout
    plan.billing_interval = "yearly"
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            provider="asaas",
            status="incomplete",
            external_checkout_id="paid-checkout",
            checkout_charge_cents=10000,
        )
    )
    await db_session.commit()
    gateway.create_checkout_session.return_value = {
        "external_checkout_id": "paid-checkout",
        "external_subscription_id": None,
        "session_id": "paid-checkout",
        "status": "completed",
        "affiliate_credit_not_applied": True,
    }
    monkeypatch.setattr(
        "app.api.v1.billing.BillingReconciliationService.reconcile_professional",
        AsyncMock(),
    )
    response = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert response.status_code == 200, response.text
    balances = await AffiliateService(db_session).balances(participant.id)
    assert balances["credit"] == 5000 and balances["reserved"] == 0
    reservation = await db_session.scalar(select(AffiliateCreditCheckout))
    assert reservation.state == "released" and reservation.source_payment_id is None


async def test_full_credit_does_not_leave_existing_charge_payable(
    api_client, db_session, professional, auth_headers, credit_checkout
):
    plan, participant, gateway = credit_checkout
    plan.price_cents = 5000
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            provider="asaas",
            status="incomplete",
            external_checkout_id="pending-external",
            checkout_charge_cents=5000,
        )
    )
    await db_session.commit()
    response = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert response.status_code == 409, response.text
    balances = await AffiliateService(db_session).balances(participant.id)
    assert balances["credit"] == 5000 and balances["reserved"] == 0
    assert await db_session.scalar(select(AffiliateCreditCheckout)) is None
    gateway.create_checkout_session.assert_not_awaited()


async def test_matching_annual_checkout_is_reused_without_cancellation():
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._get_reusable_annual_checkout = AsyncMock(
        return_value={
            "id": "matching",
            "status": "ACTIVE",
            "items": [{"quantity": 2, "value": 25}],
            "link": "https://example.com/checkout",
        }
    )
    gateway.cancel_checkout = AsyncMock()
    result = await gateway.create_checkout_session(
        account_id="account",
        plan_slug="annual",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        metadata={
            "billing_interval": "yearly",
            "price_cents": 10000,
            "charge_cents": 5000,
            "affiliate_credit_cents": 5000,
            "affiliate_credit_reused": True,
            "existing_external_checkout_id": "matching",
        },
    )
    assert result["external_checkout_id"] == "matching"
    gateway.cancel_checkout.assert_not_awaited()


async def test_renewal_gateway_uses_pending_payment_with_checkout_reference(
    monkeypatch,
):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._api_key = "test-only"
    gateway._base_url = "https://provider.invalid"
    pending = {
        "id": "pay-next",
        "status": "OVERDUE",
        "subscription": "sub-one",
        "externalReference": "account:monthly:original-session",
        "invoiceUrl": "https://example.com/next",
    }
    gateway.get_payment = AsyncMock(return_value=pending)
    gateway.list_subscription_payments = AsyncMock(
        return_value=[
            {"id": "pay-old", "status": "RECEIVED"},
            pending,
        ]
    )
    gateway.set_payment_callback = AsyncMock()
    gateway._suspend_until_first_payment = AsyncMock()
    monkeypatch.setattr(
        "app.billing.asaas_gateway.request_json", AsyncMock(return_value={})
    )
    result = await gateway.create_checkout_session(
        account_id="account",
        plan_slug="monthly",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        metadata={
            "price_cents": 10000,
            "charge_cents": 5000,
            "customer_external_id": "customer",
            "existing_external_subscription_id": "sub-one",
            "existing_external_checkout_id": "pay-next",
        },
    )
    assert result["external_checkout_id"] == "pay-next"
    assert result["status"] == "pending"
    assert not gateway._matches_external_reference(
        pending, account_id="another", plan_slug="monthly"
    )
