"""Exercise the HTTP normalizer, durable receipt and affiliate ledger together."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.affiliate import (
    AffiliateCode,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
    AffiliateReward,
)
from app.models.billing import BillingEvent, Plan, Subscription
from app.models.professional import Professional
from app.services.affiliate_billing_service import AffiliateBillingService
from app.services.affiliate_service import AffiliateService
from app.services.billing_event_recovery import recover_billing_events

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def affiliate_payment(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "asaas_webhook_token", "audit-secret")
    participant = AffiliateParticipant(
        email="payment-partner@example.com", status="active", partner_enabled=True
    )
    buyer = Professional(
        email="payment-buyer@example.com",
        password_hash="unused",
        name="Buyer",
        subscription_status="active",
        email_verified_at=datetime.now(UTC),
    )
    policy = AffiliatePolicy(
        mode="partner",
        version=1,
        status="active",
        terms_version="v1",
        commission_bps=2000,
        effective_at=datetime.now(UTC),
    )
    plan = Plan(
        slug="audit-plan", name="Audit", price_cents=10000, billing_interval="monthly"
    )
    db_session.add_all([participant, buyer, policy, plan])
    await db_session.flush()
    code = AffiliateCode(
        participant_id=participant.id,
        mode="partner",
        code="auditpaymentcode",
        terms_version="v1",
    )
    db_session.add(code)
    await db_session.flush()
    referral = AffiliateReferral(
        participant_id=participant.id,
        code_id=code.id,
        referred_professional_id=buyer.id,
        policy_id=policy.id,
        mode="partner",
        status="registered",
        policy_snapshot=policy.snapshot(),
        benefit_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    subscription = Subscription(
        professional_id=buyer.id,
        plan_id=plan.id,
        status="active",
        provider="asaas",
        external_checkout_id="pay-audit-100",
        last_payment_at=datetime.now(UTC),
    )
    db_session.add_all([referral, subscription])
    await db_session.commit()
    return participant.id, buyer.id, referral.id, subscription.id


def event(buyer_id, name="PAYMENT_RECEIVED", **payment):
    return {
        "id": "evt-" + name + "-" + str(payment.get("refunds", "")),
        "event": name,
        "dateCreated": datetime.now(UTC).isoformat(),
        "payment": {
            "id": "pay-audit-100",
            "value": 100,
            "externalReference": f"{buyer_id}:audit-plan",
            "paymentDate": datetime.now(UTC).date().isoformat(),
            **payment,
        },
    }


async def post(client, body):
    return await client.post(
        "/api/v1/billing/webhooks/asaas",
        json=body,
        headers={"asaas-access-token": "audit-secret"},
    )


async def test_duplicate_and_multiple_partial_refunds_keep_exact_balance(
    api_client, db_session, affiliate_payment
):
    participant_id, buyer_id, _, _ = affiliate_payment
    received = event(buyer_id)
    assert (await post(api_client, received)).status_code == 200
    assert (await post(api_client, received)).status_code == 200
    first = event(
        buyer_id,
        "PAYMENT_PARTIALLY_REFUNDED",
        refunds=[{"status": "DONE", "value": 25}],
    )
    assert (await post(api_client, first)).status_code == 200
    assert (await post(api_client, first)).status_code == 200
    second = event(
        buyer_id,
        "PAYMENT_PARTIALLY_REFUNDED",
        refunds=[
            {"status": "DONE", "value": 25},
            {"status": "DONE", "value": 25},
            {"status": "PENDING", "value": 50},
        ],
    )
    assert (await post(api_client, second)).status_code == 200
    rewards = (await db_session.execute(select(AffiliateReward))).scalars().all()
    assert len(rewards) == 1
    assert rewards[0].reversed_cents == 1000
    balances = await AffiliateService(db_session).balances(participant_id)
    assert balances["pending"] == 1000
    assert (
        await db_session.get(Professional, buyer_id)
    ).subscription_status == "active"


async def test_failed_webhook_recovers_on_retry(
    api_client, db_session, affiliate_payment, monkeypatch
):
    participant_id, buyer_id, _, _ = affiliate_payment
    original = AffiliateBillingService.apply
    monkeypatch.setattr(
        AffiliateBillingService,
        "apply",
        AsyncMock(side_effect=RuntimeError("test failure")),
    )
    assert (await post(api_client, event(buyer_id))).status_code == 500
    row = (await db_session.execute(select(BillingEvent))).scalar_one()
    assert row.status == "received"
    monkeypatch.setattr(AffiliateBillingService, "apply", original)
    assert (await post(api_client, event(buyer_id))).status_code == 200
    assert (await AffiliateService(db_session).balances(participant_id))[
        "pending"
    ] == 2000
    await db_session.refresh(row)
    assert row.status == "processed"


async def test_worker_recovers_without_another_provider_retry(
    api_client, db_session, affiliate_payment, monkeypatch
):
    participant_id, buyer_id, _, _ = affiliate_payment
    original = AffiliateBillingService.apply
    monkeypatch.setattr(
        AffiliateBillingService,
        "apply",
        AsyncMock(side_effect=RuntimeError("test failure")),
    )
    assert (await post(api_client, event(buyer_id))).status_code == 500
    monkeypatch.setattr(AffiliateBillingService, "apply", original)
    assert await recover_billing_events(db_session) == 1
    assert await recover_billing_events(db_session) == 0
    assert (await AffiliateService(db_session).balances(participant_id))[
        "pending"
    ] == 2000


async def test_risk_hold_records_payment_and_releases_only_after_approval(
    api_client, db_session, affiliate_payment
):
    participant_id, buyer_id, referral_id, _ = affiliate_payment
    referral = await db_session.get(AffiliateReferral, referral_id)
    referral.review_state = "manual_review"
    await db_session.commit()
    assert (await post(api_client, event(buyer_id))).status_code == 200
    service = AffiliateService(db_session)
    future = datetime.now(UTC) + timedelta(days=30)
    assert await service.release_due_rewards(now=future) == 0
    await service.review_referral(
        referral_id=referral_id, decision="approved", reason="Checked"
    )
    assert await service.release_due_rewards(now=future) == 1
    assert (await service.balances(participant_id))["available"] == 2000


async def test_partial_refund_after_subscription_checkout_changed(
    api_client, db_session, affiliate_payment
):
    participant_id, buyer_id, _, sub_id = affiliate_payment
    assert (await post(api_client, event(buyer_id))).status_code == 200
    sub = await db_session.get(Subscription, sub_id)
    sub.external_checkout_id = "pay-new-checkout"
    await db_session.commit()
    assert (
        await post(
            api_client,
            event(
                buyer_id,
                "PAYMENT_PARTIALLY_REFUNDED",
                refunds=[{"status": "DONE", "value": 50}],
            ),
        )
    ).status_code == 200
    assert (await AffiliateService(db_session).balances(participant_id))[
        "pending"
    ] == 1000
    assert sub.external_checkout_id == "pay-new-checkout"


async def test_received_date_is_not_old_card_payment_date(
    api_client, db_session, affiliate_payment
):
    _, buyer_id, _, _ = affiliate_payment
    assert (
        await post(
            api_client,
            event(buyer_id, paymentDate="2026-01-01", creditDate="2026-09-05"),
        )
    ).status_code == 200
    reward = (await db_session.execute(select(AffiliateReward))).scalar_one()
    assert reward.available_at.date().isoformat() == "2026-09-19"


async def test_late_payment_event_does_not_reactivate_refunded_subscription(
    api_client, db_session, affiliate_payment
):
    participant_id, buyer_id, _, sub_id = affiliate_payment
    assert (
        await post(api_client, event(buyer_id, "PAYMENT_REFUNDED"))
    ).status_code == 200
    sub = await db_session.get(Subscription, sub_id)
    terminal_status = sub.status
    assert terminal_status != "active"
    assert (await post(api_client, event(buyer_id))).status_code == 200
    await db_session.refresh(sub)
    assert sub.status == terminal_status
    balances = await AffiliateService(db_session).balances(participant_id)
    assert balances["pending"] == 0
    assert balances["available"] == 0
