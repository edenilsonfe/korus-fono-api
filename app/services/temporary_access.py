"""Time-bounded access exception, independent of payment and trial state."""

from datetime import UTC, datetime

from app.models.professional import Professional


def has_temporary_access(
    professional: Professional, *, now: datetime | None = None
) -> bool:
    expires_at = professional.temporary_access_ends_at
    if professional.is_disabled or expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) < expires_at


def signup_payment_blocks_access(professional: Professional) -> bool:
    return professional.signup_payment_required and not has_temporary_access(
        professional
    )
