"""Gateway registry."""

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayConfigError
from app.billing.gateway import PaymentGateway
from app.billing.stub_gateway import StubPaymentGateway
from app.core.config import get_settings


def get_payment_gateway(provider_key: str | None = None) -> PaymentGateway:
    settings = get_settings()
    key = (settings.effective_billing_provider if provider_key is None else provider_key).lower().strip()
    if key == "stub":
        return StubPaymentGateway()  # type: ignore[return-value]
    if key == "asaas":
        return AsaasPaymentGateway()  # type: ignore[return-value]
    raise PaymentGatewayConfigError("Provedor de pagamento não configurado corretamente.")


__all__ = ["PaymentGatewayConfigError", "get_payment_gateway"]
