"""LGPD-minded scrubbing for Sentry events leaving the API."""

from __future__ import annotations

import re
from typing import Any

FILTERED = "[Filtered]"

# Public clinical links carry the raw delivery token in the path (API and web).
# The home-program token travels in a header or in the web URL fragment; error
# events and breadcrumbs must never ship it: only the masked form leaves.
_TOKEN_PATH_PATTERNS = (
    re.compile(r"(/report-deliveries/)([^/?#]+)"),
    re.compile(r"(/relatorio/)([^/?#]+)"),
    # ``#token=…`` / ``?token=…`` / ``&token=…`` (F16 family link fragment).
    re.compile(r"([#?&]token=)([^&#\s]+)"),
)

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-asaas-access-token",
        "asaas-access-token",
        # F16 — raw family link token of the home program.
        "x-home-program-token",
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
        "google_calendar_client_secret",
        "google_calendar_credential_encryption_key",
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
        # F20 — public receipt: receiver identity and school authorization are
        # restricted data; the request body is dropped entirely.
        "receiver_name",
        "receivername",
        "receiver_role",
        "receiverrole",
        "received_by_name",
        "receivedbyname",
        "received_by_role",
        "receivedbyrole",
        "school_authorization",
        "schoolauthorization",
        # F16 — family link token of the home program and the family
        # authorization declaration are restricted data.
        "home_program_token",
        "homeprogramtoken",
        "family_authorization",
        "familyauthorization",
    }
)


def scrub_public_token_paths(value: str) -> str:
    """Mask raw delivery tokens carried in public path segments."""
    for pattern in _TOKEN_PATH_PATTERNS:
        value = pattern.sub(rf"\1{FILTERED}", value)
    return value


def _scrub_url_like(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_public_token_paths(value)
    return value


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
        if "url" in request:
            request["url"] = _scrub_url_like(request["url"])

    transaction = event.get("transaction")
    if isinstance(transaction, str):
        event["transaction"] = scrub_public_token_paths(transaction)

    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        values = breadcrumbs.get("values")
        if isinstance(values, list):
            for crumb in values:
                if not isinstance(crumb, dict):
                    continue
                data = crumb.get("data")
                if isinstance(data, dict):
                    for key in list(data):
                        if isinstance(data[key], str):
                            data[key] = scrub_public_token_paths(data[key])

    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {k: v for k, v in user.items() if k == "id"}

    extra = event.get("extra")
    if isinstance(extra, dict):
        for key in list(extra):
            if str(key).lower() in _SENSITIVE_EXTRA_KEYS:
                extra[key] = FILTERED

    return event
