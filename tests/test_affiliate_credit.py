"""KorusFono credit redemption and checkout reservation contracts."""

from datetime import UTC, datetime

import pytest

from app.models.affiliate import AffiliateLedgerEntry, AffiliateParticipant
from app.models.professional import Professional
from app.services.affiliate_credit_service import (
    AffiliateCreditForbiddenError,
    AffiliateCreditService,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def credit_checkout(db_session, professional, monkeypatch):
    from unittest.mock import AsyncMock

    from app.models.billing import Plan

    professional.cpf = "52998224725"
    plan = Plan(
        slug="credit-test-plan",
        name="Credit Plan",
        price_cents=10000,
        billing_interval="monthly",
    )
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        customer_enabled=True,
        status="active",
    )
    db_session.add_all([plan, participant])
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            account="credit",
            amount_cents=5000,
            entry_type="test",
            idempotency_key="credit-http-funds",
        )
    )
    await db_session.commit()
    gateway = AsyncMock()
    gateway.provider_key = "asaas"
    gateway.create_customer.return_value = {"external_customer_id": "cus-credit"}
    gateway.create_checkout_session.return_value = {
        "external_subscription_id": "sub-credit",
        "external_checkout_id": "pay-credit",
        "session_id": "pay-credit",
        "status": "pending",
    }
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    return plan, participant, gateway


async def test_credit_checkout_deducts_once_without_discounting_future_cycles(
    api_client, db_session, auth_headers, credit_checkout
):
    from sqlalchemy import select

    from app.models.billing import Subscription

    plan, participant, gateway = credit_checkout
    response = await api_client.post(
        "/api/v1/billing/checkout", headers=auth_headers, json={"planSlug": plan.slug}
    )
    assert response.status_code == 200, response.text
    assert (
        gateway.create_checkout_session.await_args.kwargs["metadata"]["charge_cents"]
        == 5000
    )
    sub = await db_session.scalar(select(Subscription))
    assert sub.checkout_recurring_price_cents == 10000
    gateway.set_recurring_price.assert_awaited_once_with(
        external_subscription_id="sub-credit", value_cents=10000
    )


@pytest.mark.parametrize("status_code", [None, 400, 500])
async def test_ambiguous_checkout_failure_keeps_credit_and_blocks_new_charge(
    api_client, db_session, auth_headers, credit_checkout, status_code
):
    from app.billing.errors import PaymentGatewayError
    from app.services.affiliate_service import AffiliateService

    plan, participant, gateway = credit_checkout
    gateway.create_checkout_session.side_effect = PaymentGatewayError(
        "failure after provider operation", status_code=status_code
    )
    assert (
        await api_client.post(
            "/api/v1/billing/checkout",
            headers=auth_headers,
            json={"planSlug": plan.slug},
        )
    ).status_code == 502
    assert (
        await api_client.post(
            "/api/v1/billing/checkout",
            headers=auth_headers,
            json={"planSlug": plan.slug},
        )
    ).status_code == 409
    gateway.create_checkout_session.assert_awaited_once()
    balances = await AffiliateService(db_session).balances(participant.id)
    assert balances["credit"] == 0
    assert balances["reserved"] == 5000


async def test_authoritative_payment_replaces_hosted_credit_binding(db_session):
    from app.services.affiliate_billing_service import AffiliateBillingService
    from app.services.affiliate_service import AffiliateService
    from tests.test_affiliate_audit_regressions import credit_account

    professional, participant = await credit_account(db_session)
    credit = AffiliateCreditService(db_session)
    await credit.reserve_for_checkout(
        professional_id=professional.id,
        charge_cents=10000,
        reservation_id="credit-hosted",
    )
    await credit.bind_payment(reservation_id="credit-hosted", payment_id="hosted-123")
    billing = AffiliateBillingService(db_session)
    payload = {
        "provider": "asaas",
        "provider_event": "PAYMENT_RECEIVED",
        "id": "pay-real",
        "value": 50,
        "external_reference": f"{professional.id}:plan:credit-hosted",
    }
    for _ in range(2):
        await billing.apply(
            payload=payload,
            event_id="real-event",
            professional_id=professional.id,
            plan_interval="yearly",
        )
    assert (await AffiliateService(db_session).balances(participant.id))[
        "reserved"
    ] == 0
    await billing.apply(
        payload={
            **payload,
            "provider_event": "PAYMENT_PARTIALLY_REFUNDED",
            "refunds": [{"value": 25, "status": "DONE"}],
        },
        event_id="partial",
        professional_id=professional.id,
        plan_interval="yearly",
    )
    assert await credit.credit_balance(professional.id) == 2500


async def test_available_reward_can_be_converted_to_credit_and_reserved(db_session):
    professional = Professional(
        email="credit-customer@example.com",
        password_hash="unused",
        name="Cliente crédito",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
        subscription_status="active",
    )
    db_session.add(professional)
    await db_session.flush()
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        status="active",
        customer_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_available",
            account="available",
            amount_cents=5000,
            idempotency_key="test:credit:available",
        )
    )
    await db_session.commit()

    service = AffiliateCreditService(db_session)
    converted = await service.convert_available_to_credit(
        professional=professional,
        amount_cents=5000,
        idempotency_key="redeem:credit:1",
    )
    assert converted == 5000
    assert await service.credit_balance(professional.id) == 5000

    reservation = await service.reserve_for_checkout(
        professional_id=professional.id,
        charge_cents=9700,
        reservation_id="checkout-credit-1",
    )
    assert reservation.applied_cents == 5000
    assert reservation.external_charge_cents == 4700
    assert await service.credit_balance(professional.id) == 0

    await service.release_checkout_reservation(reservation_id="checkout-credit-1")
    assert await service.credit_balance(professional.id) == 5000


async def test_negative_available_balance_blocks_credit_conversion(db_session):
    professional = Professional(
        email="negative-credit@example.com",
        password_hash="unused",
        name="Saldo negativo",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
        subscription_status="active",
    )
    db_session.add(professional)
    await db_session.flush()
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        status="active",
        customer_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_negative",
            account="available",
            amount_cents=-100,
            idempotency_key="test:credit:negative",
        )
    )
    await db_session.commit()

    with pytest.raises(AffiliateCreditForbiddenError, match="negativo"):
        await AffiliateCreditService(db_session).convert_available_to_credit(
            professional=professional,
            amount_cents=100,
            idempotency_key="redeem:credit:blocked",
        )
