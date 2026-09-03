"""Normalize actual payment methods, never the list of allowed checkout methods."""

from typing import Any, Literal

PaymentMethod = Literal["pix", "credit_card"]


def payment_method_from_payload(payload: dict[str, Any]) -> PaymentMethod | None:
    raw_value = (
        payload.get("billingType")
        or payload.get("billing_type")
        or payload.get("payment_method")
    )
    normalized = str(raw_value or "").strip().upper()
    if normalized == "PIX":
        return "pix"
    if normalized == "CREDIT_CARD":
        return "credit_card"
    return None
