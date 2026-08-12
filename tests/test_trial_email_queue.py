from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import trial_email_queue

pytestmark = pytest.mark.asyncio


async def test_trial_email_campaign_is_enqueued_with_deduplicated_job_id(monkeypatch):
    campaign_id = uuid4()
    redis = type("Redis", (), {})()
    redis.enqueue_job = AsyncMock()
    redis.aclose = AsyncMock()
    monkeypatch.setattr(trial_email_queue, "create_pool", AsyncMock(return_value=redis))

    await trial_email_queue.enqueue_trial_email_campaign(campaign_id)

    redis.enqueue_job.assert_awaited_once_with(
        "run_trial_email_campaign",
        str(campaign_id),
        _job_id=f"trial-email-{campaign_id}",
    )
    redis.aclose.assert_awaited_once()


async def test_trial_email_campaign_falls_back_when_redis_is_unavailable(monkeypatch):
    campaign_id = uuid4()
    monkeypatch.setattr(
        trial_email_queue,
        "create_pool",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )
    process = AsyncMock()
    monkeypatch.setattr(trial_email_queue, "process_trial_email_campaign", process)

    await trial_email_queue.enqueue_trial_email_campaign(campaign_id)

    process.assert_awaited_once_with(str(campaign_id))


async def test_trial_email_campaign_marks_failed_when_fallback_crashes(monkeypatch):
    campaign_id = uuid4()
    monkeypatch.setattr(
        trial_email_queue,
        "create_pool",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )
    monkeypatch.setattr(
        trial_email_queue,
        "process_trial_email_campaign",
        AsyncMock(side_effect=RuntimeError("provider crash")),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(
        trial_email_queue, "mark_trial_email_campaign_failed", mark_failed
    )

    await trial_email_queue.enqueue_trial_email_campaign(campaign_id)

    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.args[0] == str(campaign_id)
    assert str(mark_failed.await_args.args[1]) == "provider crash"
