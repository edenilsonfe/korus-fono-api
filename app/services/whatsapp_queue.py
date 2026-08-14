"""Schedule WhatsApp appointment notifications without blocking the HTTP response.

Default path runs after the response via FastAPI BackgroundTasks (see appointments
router). Optional ARQ path when WHATSAPP_USE_ARQ_DISPATCH=true and a worker is up.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _use_arq_dispatch() -> bool:
    raw = os.environ.get("WHATSAPP_USE_ARQ_DISPATCH", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def enqueue_whatsapp_appointment_event(
    appointment_id: UUID, notification_type: str
) -> None:
    """Run (or ARQ-enqueue) the WhatsApp dispatch. Safe for BackgroundTasks."""
    if _use_arq_dispatch():
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
            try:
                await redis.enqueue_job(
                    "dispatch_whatsapp_appointment_event",
                    str(appointment_id),
                    notification_type,
                )
                return
            finally:
                close = getattr(redis, "aclose", None) or getattr(redis, "close")
                await close()
        except Exception as exc:
            logger.warning(
                "WhatsApp ARQ enqueue failed (%s); dispatching inline for %s/%s",
                exc,
                appointment_id,
                notification_type,
            )

    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    try:
        await WhatsAppNotificationService.dispatch_appointment_event(
            appointment_id, notification_type
        )
    except Exception:
        # Background task: never let Evolution/DB errors bubble into the ASGI cycle.
        logger.exception(
            "Background WhatsApp dispatch failed for %s/%s",
            appointment_id,
            notification_type,
        )


async def enqueue_whatsapp_appointment_event_log(event_log_id: UUID) -> None:
    """Dispatch an already-persisted appointment outbox row."""
    if _use_arq_dispatch():
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
            try:
                await redis.enqueue_job(
                    "dispatch_whatsapp_appointment_event_log", str(event_log_id)
                )
                return
            finally:
                close = getattr(redis, "aclose", None) or getattr(redis, "close")
                await close()
        except Exception as exc:
            logger.warning(
                "WhatsApp outbox ARQ enqueue failed (%s); dispatching inline for %s",
                exc,
                event_log_id,
            )

    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await WhatsAppNotificationService(db).dispatch_event_log(event_log_id)
    except Exception:
        # The durable queued row remains available for the scheduler recovery pass.
        logger.exception(
            "Background WhatsApp outbox dispatch failed for %s", event_log_id
        )
