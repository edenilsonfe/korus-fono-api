"""whatsapp_queue defaults to inline dispatch (no ARQ worker in compose)."""

import os
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest

from app.services import whatsapp_queue


@pytest.mark.asyncio
async def test_enqueue_dispatches_inline_by_default(monkeypatch):
    monkeypatch.delenv("WHATSAPP_USE_ARQ_DISPATCH", raising=False)
    appointment_id = uuid4()
    dispatch = AsyncMock()
    with patch.object(
        whatsapp_queue,
        "_use_arq_dispatch",
        return_value=False,
    ), patch(
        "app.services.whatsapp_notification_service.WhatsAppNotificationService.dispatch_appointment_event",
        dispatch,
    ):
        await whatsapp_queue.enqueue_whatsapp_appointment_event(
            appointment_id, "confirmation"
        )
    dispatch.assert_awaited_once_with(appointment_id, "confirmation")


@pytest.mark.asyncio
async def test_enqueue_uses_arq_when_flag_set(monkeypatch):
    monkeypatch.setenv("WHATSAPP_USE_ARQ_DISPATCH", "true")
    appointment_id = uuid4()
    redis = AsyncMock()
    redis.enqueue_job = AsyncMock()
    redis.aclose = AsyncMock()
    create_pool = AsyncMock(return_value=redis)
    dispatch = AsyncMock()

    with patch("arq.create_pool", create_pool), patch(
        "arq.connections.RedisSettings.from_dsn", return_value=object()
    ), patch(
        "app.services.whatsapp_notification_service.WhatsAppNotificationService.dispatch_appointment_event",
        dispatch,
    ), patch.object(whatsapp_queue, "get_settings") as settings:
        settings.return_value.redis_url = "redis://localhost:6380"
        # re-read flag
        assert whatsapp_queue._use_arq_dispatch() is True
        await whatsapp_queue.enqueue_whatsapp_appointment_event(
            appointment_id, "cancelled"
        )

    redis.enqueue_job.assert_awaited_once()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbox_enqueue_dispatches_persisted_log_inline(monkeypatch):
    event_log_id = uuid4()
    dispatch = AsyncMock()

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    with patch.object(whatsapp_queue, "_use_arq_dispatch", return_value=False), patch(
        "app.db.session.AsyncSessionLocal", return_value=SessionContext()
    ), patch(
        "app.services.whatsapp_notification_service.WhatsAppNotificationService.dispatch_event_log",
        dispatch,
    ):
        await whatsapp_queue.enqueue_whatsapp_appointment_event_log(event_log_id)

    dispatch.assert_awaited_once_with(event_log_id)


@pytest.mark.asyncio
async def test_outbox_enqueue_uses_arq_event_log_job(monkeypatch):
    event_log_id = uuid4()
    redis = AsyncMock()
    redis.enqueue_job = AsyncMock()
    redis.aclose = AsyncMock()

    with patch.object(whatsapp_queue, "_use_arq_dispatch", return_value=True), patch(
        "arq.create_pool", AsyncMock(return_value=redis)
    ), patch("arq.connections.RedisSettings.from_dsn", return_value=object()), patch.object(
        whatsapp_queue, "get_settings"
    ) as settings:
        settings.return_value.redis_url = "redis://localhost:6380"
        await whatsapp_queue.enqueue_whatsapp_appointment_event_log(event_log_id)

    redis.enqueue_job.assert_awaited_once_with(
        "dispatch_whatsapp_appointment_event_log", str(event_log_id)
    )
