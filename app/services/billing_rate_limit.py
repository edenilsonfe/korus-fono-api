"""Fail-closed attempt limiting for transparent card payments."""

from app.services.auth_rate_limit import _enforce_rate_limit


def enforce_card_payment_rate_limit(*, professional_id: str, session_id: str) -> None:
    _enforce_rate_limit(
        key=f"billing:card:{professional_id}:{session_id}",
        max_requests=5,
        window_seconds=900,
        retry_after="900",
        detail="Muitas tentativas de pagamento. Aguarde alguns minutos antes de tentar novamente.",
        endpoint="billing-credit-card",
    )
