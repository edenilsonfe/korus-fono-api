from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.v1.whatsapp import refresh_evolution_connection
from app.core.config import get_settings
from app.services.evolution_whatsapp_service import EvolutionWhatsAppService


@pytest.mark.asyncio
@pytest.mark.parametrize("state, qr", [("connecting", "renewed-qr"), ("open", None)])
async def test_refresh_returns_current_qr_through_http_schema(monkeypatch, state, qr):
    monkeypatch.setattr(get_settings(), "whatsapp_provider", "evolution")
    connection = SimpleNamespace(
        status="connecting", connected_at=None, waba_id=None, phone_number_id=None,
        display_phone_number=None, verified_name=None, last_error=None,
        evolution_instance_name="test-instance",
    )
    client = SimpleNamespace(
        connection_state=AsyncMock(return_value={"instance": {"state": state}}),
        connect_instance=AsyncMock(return_value={"base64": "renewed-qr"}),
    )
    service = EvolutionWhatsAppService(AsyncMock(), client=client)
    service.get_active_connection = AsyncMock(return_value=connection)
    service._instance_api_key = lambda _: "test-key"
    service._ensure_webhook = AsyncMock()
    service._sync_phone_from_instances = AsyncMock()
    monkeypatch.setattr("app.api.v1.whatsapp.EvolutionWhatsAppService", lambda _: service)

    result = await refresh_evolution_connection(SimpleNamespace(id=uuid4()), service.db)
    wire = result.model_dump(by_alias=True)
    assert wire["qrcodeBase64"] == qr
    assert wire["connection"]["qrcodeBase64"] == qr
    assert wire["connectionState"] == state
    assert wire["canSend"] == (state == "open")
    assert client.connect_instance.await_count == (1 if state == "connecting" else 0)
