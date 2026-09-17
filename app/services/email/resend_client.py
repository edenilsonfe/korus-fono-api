"""Resend HTTP client for transactional email."""

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(
    to_email: str,
    subject: str,
    html: str,
    text: str,
    *,
    headers: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> str | None:
    settings = get_settings()
    if not settings.email_sending_enabled:
        logger.info(
            "Email sending disabled; skipping '%s' to %s",
            subject,
            to_email,
        )
        return None

    api_key = (settings.resend_api_key or "").strip()
    if not api_key:
        logger.warning(
            "RESEND_API_KEY not configured; skipping '%s' to %s",
            subject,
            to_email,
        )
        return None

    request_headers = {"Authorization": f"Bearer {api_key}"}
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    payload = {
        "from": settings.email_from,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if headers:
        payload["headers"] = headers

    response = httpx.post(
        RESEND_API_URL,
        headers=request_headers,
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
    message_id = response.json().get("id")
    logger.info(
        "Email '%s' sent to %s via Resend (id=%s)",
        subject,
        to_email,
        message_id,
    )
    return message_id
