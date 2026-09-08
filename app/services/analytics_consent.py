from datetime import UTC, datetime, timedelta

from app.models.professional import Professional

ANALYTICS_POLICY_VERSION = "2026-09-07"


def set_analytics_consent(professional: Professional, allowed: bool) -> None:
    professional.analytics_consent = allowed
    professional.analytics_consent_at = datetime.now(UTC)
    professional.analytics_consent_version = ANALYTICS_POLICY_VERSION


def has_analytics_consent(professional: Professional) -> bool:
    saved = professional.analytics_consent_at
    if not professional.analytics_consent or not saved:
        return False
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=UTC)
    return (
        professional.analytics_consent_version == ANALYTICS_POLICY_VERSION
        and datetime.now(UTC) - timedelta(days=183) <= saved <= datetime.now(UTC)
    )
