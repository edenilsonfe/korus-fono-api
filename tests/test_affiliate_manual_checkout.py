"""Manual affiliate attribution is validated before checkout side effects."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.affiliate import (
    AffiliateCode,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
)
from app.models.billing import Plan, Subscription
from app.models.feature_flag import FeatureFlag
from app.models.professional import Professional
from app.services.affiliate_service import (
    AffiliateConflictError,
    AffiliateForbiddenError,
    AffiliateService,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def manual_affiliate(db_session):
    policy = AffiliatePolicy(
        mode="partner",
        version=1,
        status="active",
        terms_version="v1",
        referral_discount_bps=1000,
        effective_at=datetime.now(UTC),
    )
    participant = AffiliateParticipant(
        email="partner-manual@example.com",
        status="active",
        partner_enabled=True,
        partner_terms_version="v1",
    )
    flag = FeatureFlag(
        key="affiliate_partner_program",
        description="test",
        enabled_global=True,
    )
    db_session.add_all([policy, participant, flag])
    await db_session.flush()
    code = AffiliateCode(
        participant_id=participant.id,
        mode="partner",
        code="partner1234",
        status="active",
        terms_version="v1",
    )
    db_session.add(code)
    await db_session.commit()
    return code


async def test_manual_referral_is_idempotent_for_same_code(
    db_session, professional, manual_affiliate
):
    service = AffiliateService(db_session)
    first = await service.register_checkout_referral(
        code=manual_affiliate.code,
        referred_professional=professional,
        request_ip="203.0.113.10",
        user_agent="test",
    )
    await db_session.commit()
    second = await service.register_checkout_referral(
        code=manual_affiliate.code,
        referred_professional=professional,
        request_ip="203.0.113.10",
        user_agent="test",
    )
    assert second.id == first.id
    assert len((await db_session.scalars(select(AffiliateReferral))).all()) == 1


async def test_manual_referral_rejects_a_different_code_after_first_assignment(
    db_session, professional, manual_affiliate
):
    service = AffiliateService(db_session)
    await service.register_checkout_referral(
        code=manual_affiliate.code,
        referred_professional=professional,
        request_ip="unknown",
        user_agent="test",
    )
    other_participant = AffiliateParticipant(
        email="other-partner@example.com",
        status="active",
        partner_enabled=True,
        partner_terms_version="v1",
    )
    db_session.add(other_participant)
    await db_session.flush()
    other_code = AffiliateCode(
        participant_id=other_participant.id,
        mode="partner",
        code="other1234",
        status="active",
        terms_version="v1",
    )
    db_session.add(other_code)
    await db_session.commit()
    with pytest.raises(AffiliateConflictError, match="já foi atribuída"):
        await service.register_checkout_referral(
            code=other_code.code,
            referred_professional=professional,
            request_ip="unknown",
            user_agent="test",
        )


async def test_manual_referral_rejects_started_checkout_and_payment_history(
    db_session, professional, manual_affiliate
):
    plan = Plan(slug="manual-checkout-plan", name="Manual", price_cents=1000)
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            status="canceled",
            last_payment_at=datetime.now(UTC),
            external_checkout_id="pay-old",
        )
    )
    await db_session.commit()
    with pytest.raises(AffiliateConflictError, match="histórico de pagamento"):
        await AffiliateService(db_session).register_checkout_referral(
            code=manual_affiliate.code,
            referred_professional=professional,
            request_ip="unknown",
            user_agent="test",
        )


async def test_manual_referral_rejects_local_incomplete_checkout_marker(
    db_session, professional, manual_affiliate
):
    plan = Plan(slug="manual-open-plan", name="Manual", price_cents=1000)
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=professional.id,
            plan_id=plan.id,
            status="incomplete",
        )
    )
    await db_session.commit()
    with pytest.raises(AffiliateConflictError, match="checkout ou assinatura"):
        await AffiliateService(db_session).register_checkout_referral(
            code=manual_affiliate.code,
            referred_professional=professional,
            request_ip="unknown",
            user_agent="test",
        )


async def test_manual_referral_rejects_disabled_flag_and_self_referral(
    db_session, professional, manual_affiliate
):
    flag = await db_session.get(FeatureFlag, "affiliate_partner_program")
    flag.enabled_global = False
    await db_session.commit()
    with pytest.raises(AffiliateForbiddenError, match="não está disponível"):
        await AffiliateService(db_session).register_checkout_referral(
            code=manual_affiliate.code,
            referred_professional=professional,
            request_ip="unknown",
            user_agent="test",
        )

    flag.enabled_global = True
    participant = await db_session.get(
        AffiliateParticipant, manual_affiliate.participant_id
    )
    participant.email = professional.email
    await db_session.commit()
    with pytest.raises(AffiliateForbiddenError, match="Autoindicação"):
        await AffiliateService(db_session).register_checkout_referral(
            code=manual_affiliate.code,
            referred_professional=professional,
            request_ip="unknown",
            user_agent="test",
        )


async def test_checkout_binds_manual_referral_before_gateway(
    api_client, db_session, professional, auth_headers, manual_affiliate, monkeypatch
):
    gateway = AsyncMock()
    gateway.provider_key = "stub"
    gateway.create_checkout_session.return_value = {
        "checkout_url": "/checkout",
        "session_id": "manual-session",
        "status": "pending",
    }
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    plan = Plan(slug="manual-http-plan", name="Manual", price_cents=1000)
    db_session.add(plan)
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={"planSlug": plan.slug, "referralCode": manual_affiliate.code},
    )
    assert response.status_code == 200, response.text
    referral = await db_session.scalar(
        select(AffiliateReferral).where(
            AffiliateReferral.referred_professional_id == professional.id
        )
    )
    assert referral is not None
    assert gateway.create_checkout_session.await_args.kwargs["metadata"]["charge_cents"] == 900
    gateway.create_checkout_session.assert_awaited_once()
    billing_me = await api_client.get("/api/v1/billing/me", headers=auth_headers)
    assert billing_me.status_code == 200
    assert billing_me.json()["referralCode"] == manual_affiliate.code


async def test_invalid_manual_code_is_rejected_before_gateway(
    api_client, auth_headers, monkeypatch
):
    gateway = AsyncMock()
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={"planSlug": "any-plan", "referralCode": "bad"},
    )
    assert response.status_code == 422
    gateway.create_checkout_session.assert_not_awaited()


async def test_checkout_rejects_auto_referral_from_matching_cpf_before_gateway(
    api_client,
    db_session,
    professional,
    auth_headers,
    manual_affiliate,
    monkeypatch,
):
    source = Professional(
        email="source-professional@example.com",
        password_hash="unused",
        name="Source",
        cpf="52998224725",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(source)
    await db_session.flush()
    participant = await db_session.get(
        AffiliateParticipant, manual_affiliate.participant_id
    )
    participant.professional_id = source.id
    gateway = AsyncMock()
    gateway.provider_key = "stub"
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: gateway)
    plan = Plan(slug="manual-self-plan", name="Manual", price_cents=1000)
    db_session.add(plan)
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/billing/checkout",
        headers=auth_headers,
        json={
            "planSlug": plan.slug,
            "referralCode": manual_affiliate.code,
            "cpf": "529.982.247-25",
        },
    )
    assert response.status_code == 403
    assert "Autoindicação" in response.json()["detail"]
    gateway.create_checkout_session.assert_not_awaited()
