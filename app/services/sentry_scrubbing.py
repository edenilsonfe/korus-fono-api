"""LGPD-minded scrubbing for Sentry events leaving the API."""

from __future__ import annotations

from typing import Any

FILTERED = "[Filtered]"

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-asaas-access-token",
        "asaas-access-token",
    }
)

_SENSITIVE_EXTRA_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "token",
        "access_token",
        "refresh_token",
        "jwt_secret",
        "asaas_api_key",
        "resend_api_key",
        "opencode_api_key",
        "audio_transcription_api_key",
        "evolution_global_api_key",
        "evolution_webhook_secret",
        "whatsapp_credential_encryption_key",
        "authorization",
        "cookie",
        "cpf",
        "billing_document",
        "billingdocument",
        "billing_cnpj",
        "billingcnpj",
        "email",
        "phone",
        "number",
        "card_number",
        "cardnumber",
        "ccv",
        "cvv",
        "credit_card",
        "creditcard",
    }
)


def scrub_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if str(key).lower() in _SENSITIVE_HEADERS:
                    headers[key] = FILTERED
        request.pop("data", None)
        request.pop("cookies", None)

    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {k: v for k, v in user.items() if k == "id"}

    extra = event.get("extra")
    if isinstance(extra, dict):
        for key in list(extra):
            if str(key).lower() in _SENSITIVE_EXTRA_KEYS:
                extra[key] = FILTERED

    return event
