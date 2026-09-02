"""HTTP-level auth matrix for POST /billing/webhooks/{provider}."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS


# ponytail: sqlite (test DB) has no native JSONB/ARRAY; same shim as
# tests/test_auth_token_version.py so this file's db_session/api_client
# fixtures work when run standalone (not just as part of the full suite).
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture(autouse=True)
def _reset_billing_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "")
    monkeypatch.setattr(settings, "debug", False)
    yield


@pytest.mark.asyncio
async def test_asaas_webhook_rejects_when_token_not_configured(api_client):
    response = await api_client.post(
        "/api/v1/billing/webhooks/asaas",
        json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1"}},
        headers={"asaas-access-token": "whatever"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_asaas_webhook_rejects_wrong_header(api_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "correct-token")

    response = await api_client.post(
        "/api/v1/billing/webhooks/asaas",
        json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1"}},
        headers={"asaas-access-token": "wrong-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_asaas_webhook_accepts_correct_header(api_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "correct-token")

    response = await api_client.post(
        "/api/v1/billing/webhooks/asaas",
        json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1"}},
        headers={"asaas-access-token": "correct-token"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_asaas_intermediate_transfer_event_does_not_fail_affiliate_payout(
    api_client, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "correct-token")
    complete_transfer = AsyncMock()

    with patch(
        "app.services.affiliate_payout_service.AffiliatePayoutService.complete_transfer",
        new=complete_transfer,
    ):
        response = await api_client.post(
            "/api/v1/billing/webhooks/asaas",
            json={
                "event": "TRANSFER_PENDING",
                "transfer": {"id": "transfer-affiliate-pending"},
            },
            headers={"asaas-access-token": "correct-token"},
        )

    assert response.status_code == 200
    complete_transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_asaas_checkout_paid_activates_the_matching_annual_subscription(
    api_client,
    db_session,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "correct-token")

    monthly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    yearly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    professional = Professional(
        email="checkout-paid@test.com",
        password_hash="hash",
        name="Checkout Paid",
        subscription_status="trialing",
        trial_started_at=datetime(2026, 8, 15, tzinfo=UTC),
        trial_ends_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    db_session.add_all([monthly_plan, yearly_plan, professional])
    await db_session.flush()

    annual_subscription = Subscription(
        professional_id=professional.id,
        plan_id=yearly_plan.id,
        status="incomplete",
        provider="asaas",
        external_checkout_id="checkout-paid-annual",
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    newer_monthly_subscription = Subscription(
        professional_id=professional.id,
        plan_id=monthly_plan.id,
        status="active",
        provider="asaas",
        external_subscription_id="sub-monthly-existing",
        external_checkout_id="pay-monthly-existing",
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    db_session.add_all([annual_subscription, newer_monthly_subscription])
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/billing/webhooks/asaas",
        json={
            "id": "evt-checkout-paid-annual",
            "event": "CHECKOUT_PAID",
            "dateCreated": "2026-08-21 21:58:25",
            "checkout": {
                "id": "checkout-paid-annual",
                "status": "PAID",
                "externalReference": f"{professional.id}:korusfono_pro_yearly",
                "chargeTypes": ["DETACHED", "INSTALLMENT"],
            },
        },
        headers={"asaas-access-token": "correct-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True, "events": 1}

    await db_session.refresh(professional)
    await db_session.refresh(annual_subscription)
    await db_session.refresh(newer_monthly_subscription)
    assert professional.subscription_status == "active"
    assert annual_subscription.status == "active"
    assert annual_subscription.last_payment_at == datetime(
        2026, 8, 21, 21, 58, 25
    )
    assert annual_subscription.current_period_end == datetime(
        2027, 8, 21, 21, 58, 25
    )
    assert newer_monthly_subscription.plan_id == monthly_plan.id
    assert newer_monthly_subscription.external_checkout_id == "pay-monthly-existing"


@pytest.mark.asyncio
async def test_asaas_deleted_future_payment_does_not_downgrade_paid_monthly_subscription(
    api_client,
    db_session,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "asaas_webhook_token", "correct-token")

    monthly_plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    professional = Professional(
        email="future-payment-deleted@test.com",
        password_hash="hash",
        name="Future Payment Deleted",
        subscription_status="active",
    )
    db_session.add_all([monthly_plan, professional])
    await db_session.flush()
    subscription = Subscription(
        professional_id=professional.id,
        plan_id=monthly_plan.id,
        status="active",
        provider="asaas",
        external_subscription_id="sub_paid_monthly",
        external_checkout_id="pay_received_monthly",
        last_payment_at=datetime(2026, 8, 25, tzinfo=UTC),
        current_period_end=datetime(2026, 9, 25, tzinfo=UTC),
    )
    db_session.add(subscription)
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/billing/webhooks/asaas",
        json={
            "event": "PAYMENT_DELETED",
            "payment": {
                "id": "pay_future_deleted",
                "status": "PENDING",
                "deleted": True,
                "dueDate": "2026-09-26",
                "subscription": "sub_paid_monthly",
                "externalReference": (
                    f"{professional.id}:korusfono_pro_monthly"
                ),
            },
        },
        headers={"asaas-access-token": "correct-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"received": True, "events": 1}

    token = create_access_token(professional.id)
    billing_me = await api_client.get(
        "/api/v1/billing/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert billing_me.status_code == 200
    assert billing_me.json()["subscriptionStatus"] == "active"
    assert billing_me.json()["subscription"]["status"] == "active"
    assert billing_me.json()["subscription"]["currentPeriodEnd"].startswith(
        "2026-09-25T00:00:00"
    )


@pytest.mark.asyncio
async def test_stub_webhook_rejects_when_not_debug(api_client):
    response = await api_client.post(
        "/api/v1/billing/webhooks/stub",
        json={"id": "evt-1", "event_type": "payment_succeeded"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stub_webhook_accepts_when_debug(api_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "debug", True)

    response = await api_client.post(
        "/api/v1/billing/webhooks/stub",
        json={"id": "evt-1", "event_type": "payment_succeeded"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_unknown_provider_webhook_404(api_client):
    response = await api_client.post(
        "/api/v1/billing/webhooks/unknown-provider",
        json={},
    )
    assert response.status_code == 404
