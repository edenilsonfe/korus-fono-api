"""Explicit review probes; expected to fail until the reported bugs are fixed.

Run directly with pytest. The filename deliberately avoids default collection.
Assertions describe required behavior; all provider operations are mocked.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.models.affiliate import AffiliatePolicy
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.affiliate_credit_service import AffiliateCreditService
from app.services.affiliate_service import AffiliateForbiddenError, AffiliateService
from tests.test_affiliate_credit import credit_checkout

pytestmark = pytest.mark.asyncio


async def test_partner_cannot_refer_own_new_clinical_account(db_session):
    service = AffiliateService(db_session)
    db_session.add(AffiliatePolicy(mode="partner", version=1, status="active",
        terms_version="review-v1", commission_bps=2000, effective_at=datetime.now(UTC)))
    await db_session.flush()
    partner = await service.invite_partner(email="self-review@example.com", public_name="Partner")
    activation = await service.activate_partner(participant=partner, terms_version="review-v1")
    own_account = Professional(email=partner.email, name="Partner", password_hash="unused")
    db_session.add(own_account)
    await db_session.flush()
    with pytest.raises(AffiliateForbiddenError):
        await service.register_referral(code=activation.code, referred_professional=own_account,
            request_ip="203.0.113.7", user_agent="review")


async def test_next_overdue_cycle_can_checkout_after_previous_credit_settled(
    api_client, db_session, professional, auth_headers, credit_checkout
):
    plan, participant, gateway = credit_checkout
    first = await api_client.post("/api/v1/billing/checkout", headers=auth_headers,
        json={"planSlug": plan.slug})
    assert first.status_code == 200
    sub = await db_session.scalar(select(Subscription))
    await AffiliateCreditService(db_session).settle_checkout_reservation(
        reservation_id=str(sub.checkout_session_id), payment_id="pay-credit")
    sub.status = "past_due"
    professional.subscription_status = "past_due"
    await db_session.commit()
    response = await api_client.post("/api/v1/billing/checkout", headers=auth_headers,
        json={"planSlug": plan.slug})
    assert response.status_code == 200, response.text


async def test_reused_annual_checkout_must_reflect_new_credit_amount(monkeypatch):
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._get_reusable_annual_checkout = AsyncMock(return_value={
        "id": "old-checkout", "status": "PENDING", "value": 100,
        "link": "https://example.com/checkout"})
    gateway.cancel_checkout = AsyncMock()
    gateway.create_hosted_annual_checkout = AsyncMock(return_value={
        "id": "new-checkout", "status": "PENDING", "link": "https://example.com/new"})
    result = await gateway.create_checkout_session(account_id="account", plan_slug="annual",
        success_url="https://example.com/success", cancel_url="https://example.com/cancel",
        metadata={"billing_interval": "yearly", "price_cents": 10000, "charge_cents": 5000,
            "affiliate_credit_cents": 5000, "existing_external_checkout_id": "old-checkout"})
    assert result["external_checkout_id"] != "old-checkout", "Old checkout still charges 100 instead of 50"
