import json
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings, validate_settings
from app.core.utils import utcnow
from app.db.session import AsyncSessionLocal
from app.models.ai import AIJob
from app.services.ai_service import run_llm
from app.services.sentry_init import init_sentry
from app.services.trial_email_campaign_service import (
    mark_trial_email_campaign_failed,
    process_trial_email_campaign,
)
from app.services.whatsapp_scheduler_service import WhatsAppSchedulerService

# Init before ARQ picks up WorkerSettings (same process as worker entry).
init_sentry(get_settings())


async def process_ai_job(ctx, job_id: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AIJob).where(AIJob.id == UUID(job_id)))
        job = result.scalar_one_or_none()
        if not job or job.status != "pending":
            return
        job.status = "processing"
        await session.commit()

        try:
            input_data = json.loads(job.input_data)
            prompt = input_data.get("prompt", str(input_data))
            result_text = await run_llm(prompt)
            job.status = "completed"
            job.result = result_text
            job.completed_at = utcnow()
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = utcnow()
        await session.commit()


async def run_whatsapp_scheduler(ctx) -> None:
    async with AsyncSessionLocal() as session:
        service = WhatsAppSchedulerService(session)
        await service.run_all()


async def dispatch_whatsapp_appointment_event(
    ctx, appointment_id: str, notification_type: str
) -> None:
    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    await WhatsAppNotificationService.dispatch_appointment_event(
        UUID(appointment_id), notification_type
    )


async def dispatch_whatsapp_appointment_event_log(ctx, event_log_id: str) -> None:
    from app.services.whatsapp_notification_service import WhatsAppNotificationService

    async with AsyncSessionLocal() as session:
        await WhatsAppNotificationService(session).dispatch_event_log(UUID(event_log_id))


async def run_trial_email_campaign(ctx, campaign_id: str) -> None:
    try:
        await process_trial_email_campaign(campaign_id)
    except Exception as exc:
        await mark_trial_email_campaign_failed(campaign_id, exc)
        raise


async def retry_google_calendar_syncs(ctx) -> None:
    from app.services.google_calendar_service import retry_pending_syncs

    await retry_pending_syncs(ctx)


async def run_affiliate_maintenance(ctx) -> None:
    from app.services.affiliate_service import AffiliateService
    from app.services.billing_event_recovery import (
        reconcile_pending_transfers,
        recover_billing_events,
    )

    async with AsyncSessionLocal() as session:
        await recover_billing_events(session)
        await reconcile_pending_transfers(session)
        await AffiliateService(session).release_due_rewards()
        await session.commit()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    functions = [
        process_ai_job,
        run_whatsapp_scheduler,
        dispatch_whatsapp_appointment_event,
        dispatch_whatsapp_appointment_event_log,
        run_trial_email_campaign,
        retry_google_calendar_syncs,
        run_affiliate_maintenance,
    ]
    cron_jobs = [
        cron(
            run_whatsapp_scheduler,
            minute={0, 15, 30, 45},
            run_at_startup=False,
        ),
        cron(retry_google_calendar_syncs, minute={5, 20, 35, 50}, run_at_startup=False),
        cron(run_affiliate_maintenance, minute=10, run_at_startup=False),
    ]

    @staticmethod
    async def on_startup(ctx):
        validate_settings(get_settings())
