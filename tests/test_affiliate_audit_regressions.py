"""Regression contracts for the affiliate production hardening."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.billing.webhook_normalizer import AsaasWebhookNormalizer
from app.models.affiliate import (
    AffiliateLedgerEntry,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReward,
)
from app.models.professional import Professional
from app.services.affiliate_credit_service import AffiliateCreditService
from app.services.affiliate_service import AffiliateService
from app.services.saas_billing_service import SaasBillingService

pytestmark = pytest.mark.asyncio


async def customer(db, email="audit@example.com"):
    professional = Professional(
        email=email,
        password_hash="unused",
        name="Audit",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        subscription_status="active",
        email_verified_at=datetime.now(UTC),
    )
    db.add(professional)
    await db.flush()
    return professional


async def credit_account(db):
    professional = await customer(db)
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        customer_enabled=True,
        status="active",
    )
    db.add(participant)
    await db.flush()
    db.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            account="credit",
            amount_cents=5000,
            entry_type="audit_seed",
            idempotency_key="audit:seed",
        )
    )
    await db.flush()
    return professional, participant


async def test_partial_refund_reaches_financial_handler():
    events = AsaasWebhookNormalizer().normalize(
        {
            "id": "evt_audit",
            "event": "PAYMENT_PARTIALLY_REFUNDED",
            "payment": {
                "id": "pay_audit",
                "value": 100,
                "refunds": [{"status": "DONE", "value": 25}],
            },
        },
        {},
    )
    assert len(events) == 1


async def test_unprocessed_webhook_can_be_retried(db_session):
    service = SaasBillingService(db_session)
    values = dict(
        provider="asaas",
        external_event_id="audit-retry",
        event_type="payment_succeeded",
        payload={},
    )
    first = await service.record_webhook_raw(**values)
    assert first.status == "received"
    retry = await service.record_webhook_raw(**values)
    assert retry is not None


async def test_checkout_retry_reserves_credit_again(db_session):
    professional, participant = await credit_account(db_session)
    service = AffiliateCreditService(db_session)
    args = dict(
        professional_id=professional.id,
        charge_cents=9700,
        reservation_id="audit-checkout",
    )
    await service.reserve_for_checkout(**args)
    await service.release_checkout_reservation(reservation_id="audit-checkout")
    retry = await service.reserve_for_checkout(**args)
    assert retry.applied_cents == 5000
    assert await service.credit_balance(professional.id) == 0


async def test_settlement_followed_by_refund_preserves_reserved_balance(db_session):
    professional, participant = await credit_account(db_session)
    service = AffiliateCreditService(db_session)
    await service.reserve_for_checkout(
        professional_id=professional.id,
        charge_cents=9700,
        reservation_id="audit-checkout",
    )
    await service.settle_checkout_reservation(reservation_id="audit-checkout")
    await service.release_checkout_reservation(reservation_id="audit-checkout")
    reserved = await db_session.scalar(
        select(func.sum(AffiliateLedgerEntry.amount_cents)).where(
            AffiliateLedgerEntry.participant_id == participant.id,
            AffiliateLedgerEntry.account == "reserved",
        )
    )
    assert reserved == 0


async def test_external_partner_can_link_customer_credit_account(db_session):
    professional = await customer(db_session)
    participant = AffiliateParticipant(
        email=professional.email, status="active", partner_enabled=True
    )
    policy = AffiliatePolicy(
        mode="customer",
        version=1,
        status="active",
        terms_version="audit-v1",
        effective_at=datetime.now(UTC),
    )
    db_session.add_all([participant, policy])
    await db_session.flush()
    result = await AffiliateService(db_session).opt_in_customer(
        professional=professional, terms_version="audit-v1"
    )
    assert result.participant.professional_id == professional.id


async def test_review_approval_recovers_paid_referral(db_session):
    referrer = await customer(db_session)
    buyer = await customer(db_session, "audit-buyer@example.com")
    policy = AffiliatePolicy(
        mode="customer",
        version=1,
        status="active",
        terms_version="audit-v1",
        customer_reward_monthly_cents=2000,
        effective_at=datetime.now(UTC),
    )
    db_session.add(policy)
    await db_session.flush()
    service = AffiliateService(db_session)
    optin = await service.opt_in_customer(
        professional=referrer, terms_version="audit-v1"
    )
    referral = await service.register_referral(
        code=optin.code,
        referred_professional=buyer,
        request_ip="192.0.2.1",
        user_agent="audit",
    )
    referral.review_state = "manual_review"
    await db_session.flush()
    reward = await service.record_external_payment(
        referred_professional_id=buyer.id,
        external_payment_id="pay_audit",
        external_event_id="evt_audit",
        provider_event="PAYMENT_RECEIVED",
        received_revenue_cents=9700,
        plan_interval="monthly",
        occurred_at=datetime.now(UTC),
    )
    assert reward is not None
    assert (await service.balances(optin.participant.id))["available"] == 0
    await service.review_referral(
        referral_id=referral.id, decision="approved", reason="Audit approved"
    )
    await service.release_due_rewards(now=datetime.now(UTC) + timedelta(days=30))
    count = await db_session.scalar(select(func.count()).select_from(AffiliateReward))
    assert count == 1
