"""PostHog server-side conversion events.

Only opaque product identifiers and commercial metadata are sent. The project
token is public by design; personal API keys must never be configured here.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class PostHogAnalyticsService:
    """Best-effort client for authoritative billing conversion events."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool((self.settings.posthog_project_token or "").strip())

    async def track_purchase(
        self,
        *,
        professional_id: str,
        plan_slug: str,
        value_cents: int,
        currency: str,
        billing_event_id: str,
        session_id: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        properties: dict[str, str | float] = {
            "$insert_id": f"purchase-{billing_event_id}",
            "transaction_id": billing_event_id,
            "plan_slug": plan_slug,
            "value": round(value_cents / 100, 2),
            "currency": currency,
            "event_source": "billing_webhook",
        }
        if session_id:
            properties["session_id"] = session_id

        payload = {
            "api_key": self.settings.posthog_project_token.strip(),
            "event": "purchase",
            "distinct_id": professional_id,
            "properties": properties,
        }
        url = f"{self.settings.posthog_host.strip().rstrip('/')}/i/v0/e/"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning(
                "PostHog purchase capture failed for billing event %s: %s",
                billing_event_id,
                exc,
            )
            return False
