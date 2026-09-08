from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.services.billing_checkout_service import BillingCheckoutService


@pytest.mark.parametrize("annual, deleted", [(False, False), (True, False), (True, True)])
async def test_pix_timeout_retry_reuses_existing_payment(annual, deleted):
    sub = SimpleNamespace(
        provider="asaas", external_checkout_id="pay_existing",
        plan=SimpleNamespace(billing_interval="yearly" if annual else "monthly", slug="pro"),
        pending_plan_id=None, external_subscription_id=None,
        billing_document="24971563792", checkout_session_id="session", id="sub",
    )
    db = AsyncMock()
    service = BillingCheckoutService(db)
    service._get_subscription = AsyncMock(return_value=sub)
    gateway = AsyncMock()
    gateway.get_payment.return_value = {"id": "pay_existing", "billingType": "PIX", "status": "PENDING", "deleted": deleted}
    gateway.list_payments_by_external_reference.return_value = []
    gateway.get_pix_qr_code.side_effect = [httpx.ReadTimeout("timeout"), {"payload": "pix-code"}]
    professional = SimpleNamespace(id="person", email="person@example.com", name="Person")
    with (
        patch("app.services.billing_checkout_service.AsaasPaymentGateway", return_value=gateway),
        patch("app.services.billing_checkout_service.BillingCustomerService") as customer,
        patch("app.services.billing_checkout_service.asaas_customer_profile", return_value={}),
    ):
        customer.return_value.ensure_customer = AsyncMock(return_value="customer")
        with pytest.raises(HTTPException) as failure:
            await service.generate_pix(session_id="session", professional=professional)
        if deleted:
            assert failure.value.status_code == 409
            gateway.create_pix_payment.assert_not_awaited()
            gateway.get_pix_qr_code.assert_not_awaited()
            return
        assert failure.value.status_code == 502
        assert "cobrança será mantida" in failure.value.detail
        assert sub.external_checkout_id == "pay_existing"
        result = await service.generate_pix(session_id="session", professional=professional)
    assert result["payload"] == "pix-code"
    assert [call.args for call in gateway.get_pix_qr_code.await_args_list] == [("pay_existing",)] * 2
    gateway.create_pix_payment.assert_not_awaited()
    gateway.delete_payment.assert_not_awaited()
    gateway.cancel_checkout.assert_not_awaited()
