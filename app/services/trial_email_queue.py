"""Durable dispatch for admin trial email campaigns with local fallback."""

import logging
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.services.trial_email_campaign_service import (
    mark_trial_email_campaign_failed,
    process_trial_email_campaign,
)

logger = logging.getLogger(__name__)


async def enqueue_trial_email_campaign(campaign_id: UUID | str) -> None:
    campaign_id = str(campaign_id)
    try:
        redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        try:
            await redis.enqueue_job(
                "run_trial_email_campaign",
                campaign_id,
                _job_id=f"trial-email-{campaign_id}",
            )
            return
        finally:
            close = getattr(redis, "aclose", None) or getattr(redis, "close")
            await close()
    except Exception:
        logger.exception(
            "Não foi possível enfileirar a campanha %s; executando no processo da API",
            campaign_id,
        )

    try:
        await process_trial_email_campaign(campaign_id)
    except Exception as exc:
        logger.exception("Falha ao processar a campanha de trial %s", campaign_id)
        await mark_trial_email_campaign_failed(campaign_id, exc)
