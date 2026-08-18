"""Billing-profile completeness and provider-safe customer metadata."""

from __future__ import annotations

import re
from typing import Any

from app.models.professional import Professional


def _digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def billing_profile_is_complete(professional: Professional) -> bool:
    return (
        len(_digits_only(professional.phone)) in {10, 11}
        and bool(professional.billing_address.strip())
        and bool(professional.billing_address_number.strip())
        and bool(professional.billing_province.strip())
        and len(_digits_only(professional.billing_postal_code)) == 8
    )


def asaas_customer_profile(professional: Professional) -> dict[str, str] | None:
    """Return Asaas customer fields only when the local profile is complete."""
    if not billing_profile_is_complete(professional):
        return None
    return {
        "customer_phone": _digits_only(professional.phone),
        "customer_address": professional.billing_address.strip(),
        "customer_address_number": professional.billing_address_number.strip(),
        "customer_complement": professional.billing_address_complement.strip(),
        "customer_province": professional.billing_province.strip(),
        "customer_postal_code": _digits_only(professional.billing_postal_code),
    }
