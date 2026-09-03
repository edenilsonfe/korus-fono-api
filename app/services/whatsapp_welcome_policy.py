"""Shared delivery and retry policy for registration welcome messages."""

from datetime import UTC, datetime, timedelta

WELCOME_NOTIFICATION_TYPE = "registration_welcome"
MAX_WELCOME_SEND_ATTEMPTS = 1


def welcome_retry_at(attempt_count: int, *, now: datetime | None = None) -> datetime:
    base = now or datetime.now(UTC)
    delay_minutes = 5 * (3 ** max(0, attempt_count - 1))
    return base + timedelta(minutes=delay_minutes)
