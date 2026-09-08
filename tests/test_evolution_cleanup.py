from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.services.evolution_api_client import EvolutionApiError
from app.services.evolution_whatsapp_service import EvolutionWhatsAppService


@pytest.mark.parametrize("caller", ["disconnect", "_soft_disconnect_existing"])
@pytest.mark.parametrize("delete_status", [400, 404, None])
async def test_cleanup_only_disconnects_after_remote_removal(caller, delete_status):
    connection = SimpleNamespace(
        evolution_instance_name="test-instance", status="connecting", disconnected_at=None
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [connection])
    )
    client = SimpleNamespace(
        logout_instance=AsyncMock(side_effect=EvolutionApiError("logout failed", status_code=500)),
        delete_instance=AsyncMock(side_effect=(
            EvolutionApiError("delete failed", status_code=delete_status)
            if delete_status else None
        )),
    )
    service = EvolutionWhatsAppService(db, client=client)
    service.get_active_connection = AsyncMock(return_value=connection)
    service._instance_api_key = lambda _: "test-key"
    if delete_status == 400:
        with pytest.raises(HTTPException) as exc:
            await getattr(service, caller)(uuid4())
        assert exc.value.status_code == 502
        assert connection.status == "connecting"
        assert connection.disconnected_at is None
        db.commit.assert_not_awaited()
        db.flush.assert_not_awaited()
    else:
        await getattr(service, caller)(uuid4())
        assert connection.status == "disconnected"
        assert connection.disconnected_at is not None
    client.delete_instance.assert_awaited_once_with("test-instance", api_key="test-key")


async def test_missing_credentials_does_not_claim_disconnection(monkeypatch):
    monkeypatch.setattr(get_settings(), "evolution_global_api_key", "")
    connection = SimpleNamespace(
        evolution_instance_name="test-instance", status="connecting", disconnected_at=None,
        encrypted_instance_api_key=None, encrypted_access_token=None,
    )
    service = EvolutionWhatsAppService(AsyncMock(), client=AsyncMock())
    service.get_active_connection = AsyncMock(return_value=connection)
    with pytest.raises(HTTPException) as exc:
        await service.disconnect(uuid4())
    assert exc.value.status_code == 409
    assert connection.status == "connecting"
    service.db.commit.assert_not_awaited()
    service.client.delete_instance.assert_not_awaited()
