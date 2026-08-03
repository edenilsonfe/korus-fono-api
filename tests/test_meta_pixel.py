"""Tests for the Meta Pixel integration (Conversions API / server-side events)."""

import hashlib

import httpx
import pytest

from app.core.config import get_settings
from app.services.meta_pixel_service import MetaPixelService


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


async def test_pixel_config_public_and_disabled_by_default(api_client):
    response = await api_client.get("/api/v1/tracking/pixel-config")
    assert response.status_code == 200
    body = response.json()
    assert body["pixelId"] == ""
    assert body["enabled"] is False


async def test_pixel_config_with_pixel_id(api_client, monkeypatch):
    monkeypatch.setenv("META_PIXEL_ID", "1234567890")
    get_settings.cache_clear()
    try:
        response = await api_client.get("/api/v1/tracking/pixel-config")
        assert response.status_code == 200
        assert response.json()["pixelId"] == "1234567890"
        assert response.json()["enabled"] is False
    finally:
        get_settings.cache_clear()


def test_build_user_data_hashes_pii():
    service = MetaPixelService()
    data = service.build_user_data(
        email="Cliente@Example.COM",
        phone="+55 (11) 99999-0000",
        first_name="Maria",
        last_name="Silva",
        client_ip="200.1.2.3",
        client_user_agent="pytest",
        fbp="fb.1.1.1",
        fbc="fb.1.1.2",
    )
    assert data["em"] == [_sha256("cliente@example.com")]
    assert data["ph"] == [_sha256("+55 (11) 99999-0000")]
    assert data["em"][0] != "cliente@example.com"
    assert data["fn"] == [_sha256("maria")]
    assert data["ln"] == [_sha256("silva")]
    assert data["client_ip_address"] == "200.1.2.3"
    assert data["client_user_agent"] == "pytest"
    assert data["fbp"] == "fb.1.1.1"
    assert data["fbc"] == "fb.1.1.2"


async def test_send_event_disabled_when_not_configured(monkeypatch):
    monkeypatch.delenv("META_PIXEL_ID", raising=False)
    monkeypatch.delenv("META_CAPI_ACCESS_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        service = MetaPixelService()
        sent = await service.send_event(
            event_name="Purchase",
            event_id="purchase-x",
            user_data={"em": ["abc"]},
        )
        assert sent is False
    finally:
        get_settings.cache_clear()


async def test_send_event_posts_to_graph_api(monkeypatch):
    monkeypatch.setenv("META_PIXEL_ID", "123")
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", "tok-abc")
    get_settings.cache_clear()

    captured: dict = {}

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse({"events_received": 1})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    try:
        service = MetaPixelService()
        sent = await service.track_purchase(
            professional_id="pro-1",
            email="cliente@example.com",
            name="Cliente Teste",
            value_cents=4990,
            currency="BRL",
            plan_slug="pro",
            billing_event_id="evt-1",
        )
        assert sent is True
    finally:
        get_settings.cache_clear()

    assert captured["url"] == "https://graph.facebook.com/v21.0/123/events"
    payload = captured["payload"]
    assert payload["access_token"] == "tok-abc"
    event = payload["data"][0]
    assert event["event_name"] == "Purchase"
    assert event["event_id"] == "purchase-evt-1"
    assert event["action_source"] == "website"
    assert event["user_data"]["em"] == [_sha256("cliente@example.com")]
    assert event["custom_data"]["value"] == 49.9
    assert event["custom_data"]["content_ids"] == ["pro"]


async def test_send_event_failure_is_best_effort(monkeypatch):
    monkeypatch.setenv("META_PIXEL_ID", "123")
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", "tok-abc")
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
        service = MetaPixelService()
        sent = await service.send_event(
            event_name="Purchase",
            event_id="purchase-x",
            user_data={"em": ["abc"]},
        )
        assert sent is False
    finally:
        get_settings.cache_clear()


async def test_forward_event_requires_auth(api_client):
    response = await api_client.post(
        "/api/v1/tracking/events",
        json={"eventName": "ViewContent", "eventId": "web-1"},
    )
    assert response.status_code == 401


async def test_forward_event_ok_when_disabled(api_client, auth_headers):
    response = await api_client.post(
        "/api/v1/tracking/events",
        headers=auth_headers,
        json={
            "eventName": "ViewContent",
            "eventId": "web-1",
            "eventSourceUrl": "https://app.korusfono.com.br/pacientes",
            "customData": {"page": "patients"},
            "fbp": "fb.1.1.1",
            "fbc": "fb.1.1.2",
        },
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Evento rastreado"


async def test_register_schedules_tracking_tasks(api_client, monkeypatch):
    """Register must enqueue CompleteRegistration/StartTrial as background tasks."""
    scheduled: list[tuple[str, dict]] = []

    async def fake_track_registration(self, **kwargs):
        scheduled.append(("CompleteRegistration", kwargs))
        return True

    async def fake_track_start_trial(self, **kwargs):
        scheduled.append(("StartTrial", kwargs))
        return True

    monkeypatch.setattr(MetaPixelService, "track_registration", fake_track_registration)
    monkeypatch.setattr(MetaPixelService, "track_start_trial", fake_track_start_trial)

    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": "novo-profissional@example.com",
            "password": "senha-forte-123",
            "name": "Nova Profissional",
            "specialtyKey": "fono",
            "council": "CREFITO-3",
        },
    )
    assert response.status_code == 201
    assert {name for name, _ in scheduled} == {"CompleteRegistration", "StartTrial"}
    assert scheduled[0][1]["email"] == "novo-profissional@example.com"
