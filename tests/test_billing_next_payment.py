"""Renewal invoices are readable before expiry and never recreate a charge."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayError
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS


@pytest.mark.parametrize("account_status", ["active", "past_due"])
async def test_next_payment_is_owned_pending_and_read_only(
    db_session, professional, auth_headers, api_client, account_status,
):
    professional.subscription_status = account_status
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add(plan)
    await db_session.flush()
    original_session = uuid4()
    sub = Subscription(
        professional_id=professional.id, plan_id=plan.id, provider="asaas",
        status=account_status, external_subscription_id="sub_owned",
        external_checkout_id="pay_original", checkout_session_id=original_session,
        last_payment_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    db_session.add(sub)
    await db_session.commit()
    invoice = {
        "id": "pay_next", "subscription": "sub_owned", "status": "PENDING",
        "dueDate": "2026-09-13", "value": 97.90, "billingType": "PIX",
        "invoiceUrl": "https://www.asaas.com/i/next",
    }
    gateway = AsyncMock()
    gateway.list_subscription_payments.return_value = [
        {**invoice, "id": "pay_later", "dueDate": "2026-10-13"},
        *[{**invoice, "id": "pay_ignore", "status": status, "dueDate": "2026-08-13"}
          for status in ["RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH", "REFUNDED", "AWAITING_RISK_ANALYSIS"]],
        {**invoice, "id": "pay_deleted", "deleted": True, "dueDate": "2026-08-13"},
        {**invoice, "id": "pay_other", "subscription": "sub_other", "dueDate": "2026-08-13"},
        invoice,
    ]
    with (
        patch("app.services.billing_checkout_service.AsaasPaymentGateway", return_value=gateway),
        patch("app.services.billing_checkout_service.datetime") as clock,
    ):
        # Sep 11 in UTC is still Sep 10 in Sao Paulo.
        clock.now.return_value = datetime.fromisoformat("2026-09-10T22:30:00-03:00")
        response = await api_client.get("/api/v1/billing/next-payment", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {
            "paymentId": "pay_next", "dueDate": "2026-09-13", "daysUntilDue": 3,
            "amountCents": 9790, "paymentMethod": "pix",
            "invoiceUrl": "https://www.asaas.com/i/next",
        }
        assert str(clock.now.call_args.args[0]) == "America/Sao_Paulo"
        gateway.list_subscription_payments.assert_awaited_once_with("sub_owned")
        await db_session.refresh(sub)
        await db_session.refresh(professional)
        assert sub.external_checkout_id == "pay_original"
        assert sub.checkout_session_id == original_session
        assert sub.status == professional.subscription_status == account_status

        gateway.list_subscription_payments.return_value = [{**invoice, "status": "CONFIRMED"}]
        assert (await api_client.get("/api/v1/billing/next-payment", headers=auth_headers)).json() is None

        for invalid in [
            {"invoiceUrl": "javascript:alert(1)"}, {"dueDate": "invalid"}, {"value": "NaN"},
        ]:
            gateway.list_subscription_payments.return_value = [{**invoice, **invalid}]
            assert (await api_client.get("/api/v1/billing/next-payment", headers=auth_headers)).status_code == 502
        gateway.list_subscription_payments.side_effect = httpx.ReadTimeout("timeout")
        failure = await api_client.get("/api/v1/billing/next-payment", headers=auth_headers)
        assert failure.status_code == 502
        assert "Tente novamente" in failure.json()["detail"]


async def test_next_payment_requires_auth_and_does_not_read_another_account(
    db_session, professional, auth_headers, api_client,
):
    professional.subscription_status = "active"
    other = Professional(email="other-billing@test.com", name="Other", password_hash="test")
    plan = Plan(**COMMERCIAL_PLAN_SEEDS[0])
    db_session.add_all([other, plan])
    await db_session.flush()
    db_session.add(Subscription(
        professional_id=other.id, plan_id=plan.id, status="active", provider="asaas",
        external_subscription_id="sub_other",
    ))
    await db_session.commit()
    with patch("app.services.billing_checkout_service.AsaasPaymentGateway") as gateway:
        assert (await api_client.get("/api/v1/billing/next-payment")).status_code == 401
        response = await api_client.get("/api/v1/billing/next-payment", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() is None
        gateway.assert_not_called()


async def test_subscription_payments_reads_all_pages():
    gateway = object.__new__(AsaasPaymentGateway)
    gateway._base_url = "https://api-sandbox.asaas.com/v3"
    gateway._api_key = "test-only"
    with patch("app.billing.asaas_gateway.request_json", new_callable=AsyncMock) as request:
        request.side_effect = [
            {"data": [{"id": "paid"}], "hasMore": True},
            {"data": [{"id": "next"}], "hasMore": False},
        ]
        assert await gateway.list_subscription_payments("sub_test") == [{"id": "paid"}, {"id": "next"}]
        assert request.await_args_list[0].args[1].endswith("limit=100&offset=0")
        assert request.await_args_list[1].args[1].endswith("limit=100&offset=1")
        request.side_effect = None
        request.return_value = {"data": [], "hasMore": True}
        with pytest.raises(PaymentGatewayError):
            await gateway.list_subscription_payments("sub_test")
