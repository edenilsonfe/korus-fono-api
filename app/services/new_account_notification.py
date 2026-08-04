"""Notification email sent to the platform owner when a new account is created."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services.email.resend_client import send_email
from app.services.email.templates import new_account_notification_email

logger = logging.getLogger(__name__)


def send_new_account_notification_sync(
    *,
    user_name: str,
    user_email: str,
    specialty: str,
    council: str,
    phone: str,
    created_at: datetime,
    trial_ends_at: datetime | None,
) -> None:
    """Notify the platform owner about a newly created account (fire-and-forget).

    Skipped (with log) when no recipient is configured or email sending is disabled.
    """
    settings = get_settings()
    recipient = (settings.new_account_notification_email or "").strip()
    if not recipient:
        logger.info("New account notification email not configured; skipping")
        return
    if not settings.email_sending_enabled:
        logger.info(
            "Email sending disabled; skipping new account notification for %s",
            user_email,
        )
        return

    tz = ZoneInfo(settings.clinic_timezone)
    created_label = created_at.astimezone(tz).strftime("%d/%m/%Y %H:%M")
    trial_label = trial_ends_at.astimezone(tz).strftime("%d/%m/%Y") if trial_ends_at else "—"

    rendered = new_account_notification_email(
        user_name=user_name,
        user_email=user_email,
        specialty=specialty,
        council=council,
        phone=phone,
        created_at=created_label,
        trial_ends_at=trial_label,
    )
    try:
        send_email(
            to_email=recipient,
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
        )
    except Exception as exc:
        logger.exception("Failed to send new account notification: %s", exc)
