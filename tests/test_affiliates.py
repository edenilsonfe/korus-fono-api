"""Affiliate domain, attribution, ledger and administration contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.admin_permissions import (
    PERMISSION_AFFILIATES_PAYOUT,
    PERMISSION_AFFILIATES_READ,
    PERMISSION_AFFILIATES_WRITE,
    permissions_for_role,
)
from app.core.security import create_access_token, hash_password
from app.models.affiliate import (
    AffiliateCode,
    AffiliateLedgerEntry,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
    AffiliateReward,
)
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.affiliate_service import (
    AffiliateConflictError,
    AffiliateForbiddenError,
    AffiliateNotFoundError,
    AffiliateService,
)
from app.services.saas_billing_service import _payment_value_cents

pytestmark = pytest.mark.asyncio


async def test_provider_payment_value_is_authoritative_for_recurring_commission():
    assert _payment_value_cents({"value": 85.37}, fallback_cents=9300) == 8537
    assert _payment_value_cents({"value": "120.00"}, fallback_cents=9300) == 12000
    assert _payment_value_cents({"value": "invalid"}, fallback_cents=9300) == 9300


def _auth(professional: Professional) -> dict[str, str]:
    token = create_access_token(professional.id, professional.token_version)
    return {"Authorization": f"Bearer {token}"}


async def _professional(db, *, email: str, **values) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=values.get("name", email),
        specialty_key="fono",
        specialty="Fonoaudiologia",
        phone=values.get("phone", "11999990000"),
        cpf=values.get("cpf", ""),
        email_verified_at=values.get("email_verified_at", datetime.now(UTC)),
        subscription_status=values.get("subscription_status", "trialing"),
        is_staff=values.get("is_staff", False),
        admin_role=values.get("admin_role"),
    )
    db.add(professional)
    await db.flush()
    return professional


async def _active_policy(db, *, mode: str, version: int = 1) -> AffiliatePolicy:
    policy = AffiliatePolicy(
        mode=mode,
        version=version,
        status="active",
        terms_version=f"{mode}-v{version}",
        referral_discount_bps=1000 if mode == "customer" else 1500,
        commission_bps=2000 if mode == "partner" else 0,
        customer_reward_monthly_cents=2000,
        customer_reward_quarterly_cents=5000,
        customer_reward_yearly_cents=15000,
        effective_at=datetime.now(UTC),
    )
    db.add(policy)
    await db.flush()
    return policy


async def test_permissions_are_limited_to_billing_and_superadmin():
    billing = permissions_for_role("billing")
    assert {
        PERMISSION_AFFILIATES_READ,
        PERMISSION_AFFILIATES_WRITE,
        PERMISSION_AFFILIATES_PAYOUT,
    }.issubset(billing)
    assert PERMISSION_AFFILIATES_READ not in permissions_for_role("support")
    assert PERMISSION_AFFILIATES_WRITE not in permissions_for_role("product")


async def test_customer_opt_in_requires_verified_eligible_account(db_session):
    policy = await _active_policy(db_session, mode="customer")
    eligible = await _professional(db_session, email="eligible-affiliate@example.com")
    await db_session.commit()

    result = await AffiliateService(db_session).opt_in_customer(
        professional=eligible,
        terms_version=policy.terms_version,
    )
    await db_session.commit()

    assert result.mode == "customer"
    assert result.code
    assert result.participant.status == "active"
    assert result.participant.professional_id == eligible.id

    policy.status = "retired"
    next_policy = await _active_policy(db_session, mode="customer", version=2)
    next_policy.terms_version = "customer-v2"
    await db_session.flush()
    with pytest.raises(AffiliateNotFoundError, match="indisponível"):
        await AffiliateService(db_session).resolve_public_code(result.code)
    renewed = await AffiliateService(db_session).opt_in_customer(
        professional=eligible,
        terms_version=next_policy.terms_version,
    )
    assert renewed.code == result.code
    assert (
        await AffiliateService(db_session).resolve_public_code(renewed.code)
    )["code"] == renewed.code

    expired = await _professional(
        db_session,
        email="expired-affiliate@example.com",
        subscription_status="trial_expired",
    )
    await db_session.commit()
    with pytest.raises(AffiliateForbiddenError, match="assinatura elegível"):
        await AffiliateService(db_session).opt_in_customer(
            professional=expired,
            terms_version=policy.terms_version,
        )


async def test_attribution_is_first_valid_immutable_and_blocks_self_referral(db_session):
    policy = await _active_policy(db_session, mode="customer")
    referrer = await _professional(
        db_session,
        email="referrer@example.com",
        cpf="52998224725",
    )
    service = AffiliateService(db_session)
    opted_in = await service.opt_in_customer(
        professional=referrer,
        terms_version=policy.terms_version,
    )
    referred = await _professional(
        db_session,
        email="referred@example.com",
        cpf="11144477735",
    )
    await db_session.flush()

    referral = await service.register_referral(
        code=opted_in.code,
        referred_professional=referred,
        request_ip="198.51.100.12",
        user_agent="pytest",
    )
    assert referral.policy_snapshot["referralDiscountBps"] == 1000
    assert referral.referred_professional_id == referred.id

    same_device_account = await _professional(
        db_session,
        email="same-device-referred@example.com",
        cpf="39053344705",
        phone="11888887777",
    )
    same_device_referral = await service.register_referral(
        code=opted_in.code,
        referred_professional=same_device_account,
        request_ip="198.51.100.12",
        user_agent="pytest",
    )
    assert same_device_referral.review_state == "manual_review"
    assert same_device_referral.review_reason == "Dispositivo ou rede coincidente"

    other_referrer = await _professional(db_session, email="other-referrer@example.com")
    other = await service.opt_in_customer(
        professional=other_referrer,
        terms_version=policy.terms_version,
    )
    with pytest.raises(AffiliateConflictError, match="já foi atribuída"):
        await service.register_referral(
            code=other.code,
            referred_professional=referred,
            request_ip="198.51.100.13",
            user_agent="pytest",
        )

    referred.cpf = "529.982.247-25"
    discount_bps, rejected_referral = await service.referral_discount(referred.id)
    assert discount_bps == 0
    assert rejected_referral is referral
    assert referral.status == "rejected"
    assert referral.review_state == "rejected"

    self_account = await _professional(
        db_session,
        email="self-referral@example.com",
        cpf="529.982.247-25",
    )
    with pytest.raises(AffiliateForbiddenError, match="[Aa]utoindicação"):
        await service.register_referral(
            code=opted_in.code,
            referred_professional=self_account,
            request_ip="198.51.100.14",
            user_agent="pytest",
        )


async def test_payment_received_moves_reward_after_cooling_off_and_is_idempotent(db_session):
    policy = await _active_policy(db_session, mode="partner")
    participant = AffiliateParticipant(
        email="partner@example.com",
        public_name="Parceiro aprovado",
        status="active",
        partner_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    code = AffiliateCode(
        participant_id=participant.id,
        mode="partner",
        code="partneropaque123",
        status="active",
        terms_version=policy.terms_version,
    )
    db_session.add(code)
    referred = await _professional(db_session, email="buyer@example.com")
    plan = Plan(
        slug="affiliate_monthly",
        name="Mensal",
        price_cents=10000,
        billing_interval="monthly",
    )
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=referred.id,
            plan_id=plan.id,
            status="active",
            provider="asaas",
        )
    )
    referral = AffiliateReferral(
        participant_id=participant.id,
        code_id=code.id,
        referred_professional_id=referred.id,
        policy_id=policy.id,
        mode="partner",
        status="converted",
        policy_snapshot=policy.snapshot(),
        benefit_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(referral)
    await db_session.commit()

    service = AffiliateService(db_session)
    reward = await service.record_external_payment(
        referred_professional_id=referred.id,
        external_payment_id="pay_affiliate_1",
        external_event_id="asaas-PAYMENT_CONFIRMED-pay_affiliate_1",
        provider_event="PAYMENT_CONFIRMED",
        received_revenue_cents=8500,
        plan_interval="monthly",
        occurred_at=datetime.now(UTC),
    )
    assert reward is not None
    assert reward.state == "pending"
    assert reward.gross_cents == 1700

    same_reward = await service.record_external_payment(
        referred_professional_id=referred.id,
        external_payment_id="pay_affiliate_1",
        external_event_id="asaas-PAYMENT_RECEIVED-pay_affiliate_1",
        provider_event="PAYMENT_RECEIVED",
        received_revenue_cents=8500,
        plan_interval="monthly",
        occurred_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert same_reward.id == reward.id
    assert same_reward.state == "coolingOff"
    assert same_reward.available_at is not None
    rewards = (
        await db_session.execute(select(AffiliateReward))
    ).scalars().all()
    assert len(rewards) == 1
    pending_entries = (
        await db_session.execute(
            select(AffiliateLedgerEntry).where(
                AffiliateLedgerEntry.idempotency_key == "reward:pay_affiliate_1:pending"
            )
        )
    ).scalars().all()
    assert len(pending_entries) == 1

    reversed_reward = await service.reverse_external_payment(
        external_payment_id="pay_affiliate_1",
        external_event_id="asaas-PAYMENT_PARTIALLY_REFUNDED-pay_affiliate_1",
        reversed_revenue_cents=4250,
    )
    assert reversed_reward is reward
    assert reward.reversed_cents == 850
    released = await service.release_due_rewards(
        now=reward.available_at + timedelta(seconds=1)
    )
    balances = await service.balances(participant.id)
    assert released == 1
    assert balances["pending"] == 0
    assert balances["available"] == 850


async def test_admin_overview_and_policy_creation_are_permission_guarded(
    api_client, db_session
):
    await _active_policy(db_session, mode="customer")
    billing_admin = await _professional(
        db_session,
        email="affiliate-admin@example.com",
        is_staff=True,
        admin_role="billing",
    )
    support_admin = await _professional(
        db_session,
        email="affiliate-support@example.com",
        is_staff=True,
        admin_role="support",
    )
    await db_session.commit()

    forbidden = await api_client.get(
        "/api/v1/admin/affiliates/overview",
        headers=_auth(support_admin),
    )
    assert forbidden.status_code == 403

    overview = await api_client.get(
        "/api/v1/admin/affiliates/overview",
        headers=_auth(billing_admin),
    )
    assert overview.status_code == 200
    assert overview.json()["activePolicies"] == 1

    created = await api_client.post(
        "/api/v1/admin/affiliates/policies",
        headers=_auth(billing_admin),
        json={
            "mode": "partner",
            "termsVersion": "partner-v1",
            "referralDiscountBps": 1500,
            "commissionBps": 2000,
            "customerRewardMonthlyCents": 2000,
            "customerRewardQuarterlyCents": 5000,
            "customerRewardYearlyCents": 15000,
            "effectiveAt": datetime.now(UTC).isoformat(),
            "activate": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["version"] == 1
    assert created.json()["status"] == "active"


async def test_rejecting_risk_review_voids_unreleased_reward_without_mutating_history(
    db_session,
):
    policy = await _active_policy(db_session, mode="partner")
    participant = AffiliateParticipant(
        email="risk-partner@example.com",
        status="active",
        partner_enabled=True,
    )
    referred = await _professional(db_session, email="risk-buyer@example.com")
    db_session.add(participant)
    await db_session.flush()
    code = AffiliateCode(
        participant_id=participant.id,
        mode="partner",
        code="riskpartneropaque",
        status="active",
        terms_version=policy.terms_version,
    )
    db_session.add(code)
    await db_session.flush()
    referral = AffiliateReferral(
        participant_id=participant.id,
        code_id=code.id,
        referred_professional_id=referred.id,
        policy_id=policy.id,
        mode="partner",
        status="converted",
        review_state="manual_review",
        policy_snapshot=policy.snapshot(),
        benefit_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(referral)
    await db_session.flush()
    reward = AffiliateReward(
        participant_id=participant.id,
        referral_id=referral.id,
        source_payment_id="pay-risk-review",
        source_event_id="event-risk-review",
        kind="partner_recurring",
        state="coolingOff",
        gross_cents=2000,
        external_revenue_cents=10000,
        available_at=datetime.now(UTC) + timedelta(days=14),
    )
    db_session.add(reward)
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            reward_id=reward.id,
            entry_type="reward_pending",
            account="pending",
            amount_cents=2000,
            idempotency_key="reward:pay-risk-review:pending",
        )
    )
    await db_session.flush()

    reviewed = await AffiliateService(db_session).review_referral(
        referral_id=referral.id,
        decision="rejected",
        reason="Documento incompatível com a titularidade validada",
    )
    balances = await AffiliateService(db_session).balances(participant.id)

    assert reviewed.status == "rejected"
    assert reviewed.review_state == "rejected"
    assert reward.state == "voided"
    assert balances["pending"] == 0
    assert len(
        (
            await db_session.execute(
                select(AffiliateLedgerEntry).where(
                    AffiliateLedgerEntry.reward_id == reward.id
                )
            )
        ).scalars().all()
    ) == 2


async def test_admin_list_never_exposes_fiscal_secrets(api_client, db_session):
    await _active_policy(db_session, mode="partner")
    admin = await _professional(
        db_session,
        email="affiliate-safe-admin@example.com",
        is_staff=True,
        admin_role="billing",
    )
    participant = AffiliateParticipant(
        email="safe-partner@example.com",
        public_name="Parceiro seguro",
        status="active",
        partner_enabled=True,
    )
    db_session.add(participant)
    await db_session.commit()

    response = await api_client.get(
        "/api/v1/admin/affiliates/participants",
        headers=_auth(admin),
    )
    assert response.status_code == 200
    serialized = response.text.lower()
    assert "encrypted" not in serialized
    assert "pixkey" not in serialized
    assert "cpf" not in serialized
