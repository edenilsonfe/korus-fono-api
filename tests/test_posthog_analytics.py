"""PostHog server-side conversion events."""

import httpx

from app.core.config import get_settings
from app.services.posthog_analytics_service import PostHogAnalyticsService


async def test_purchase_is_disabled_without_project_token(monkeypatch):
    monkeypatch.delenv("POSTHOG_PROJECT_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        sent = await PostHogAnalyticsService().track_purchase(
            professional_id="professional-1",
            plan_slug="korusfono_pro_monthly",
            value_cents=9790,
            currency="BRL",
            billing_event_id="asaas-PAYMENT_CONFIRMED-pay-1",
            session_id="checkout-session-1",
        )
    finally:
        get_settings.cache_clear()

    assert sent is False


async def test_purchase_posts_identified_idempotent_event_without_personal_data(monkeypatch):
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    get_settings.cache_clear()
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    try:
        sent = await PostHogAnalyticsService().track_purchase(
            professional_id="professional-1",
            plan_slug="korusfono_pro_yearly",
            value_cents=93000,
            currency="BRL",
            billing_event_id="asaas-PAYMENT_CONFIRMED-pay-1",
            session_id="checkout-session-1",
        )
    finally:
        get_settings.cache_clear()

    assert sent is True
    assert captured["url"] == "https://us.i.posthog.com/i/v0/e/"
    assert captured["timeout"] == 5.0
    assert captured["payload"] == {
        "api_key": "phc_test",
        "event": "purchase",
        "distinct_id": "professional-1",
        "properties": {
            "$insert_id": "purchase-asaas-PAYMENT_CONFIRMED-pay-1",
            "transaction_id": "asaas-PAYMENT_CONFIRMED-pay-1",
            "plan_slug": "korusfono_pro_yearly",
            "value": 930.0,
            "currency": "BRL",
            "session_id": "checkout-session-1",
            "event_source": "billing_webhook",
        },
    }
    serialized = str(captured["payload"]).lower()
    assert "email" not in serialized
    assert "cpf" not in serialized
    assert "card" not in serialized


async def test_purchase_capture_failure_is_best_effort(monkeypatch):
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    get_settings.cache_clear()

    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)
    try:
        sent = await PostHogAnalyticsService().track_purchase(
            professional_id="professional-1",
            plan_slug="korusfono_pro_monthly",
            value_cents=9790,
            currency="BRL",
            billing_event_id="evt-1",
        )
    finally:
        get_settings.cache_clear()

    assert sent is False
