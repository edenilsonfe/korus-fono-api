from unittest.mock import Mock

import pytest

import worker


@pytest.mark.asyncio
async def test_worker_validates_runtime_settings_on_startup(monkeypatch):
    settings = object()
    validate_settings = Mock()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "validate_settings", validate_settings, raising=False)

    await worker.WorkerSettings.on_startup({})

    validate_settings.assert_called_once_with(settings)
