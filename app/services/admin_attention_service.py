from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.admin_permissions import (
    PERMISSION_BILLING_READ,
    PERMISSION_MESSAGING_READ,
    PERMISSION_PRODUCT_READ,
)
from app.models.ai import AIJob
from app.models.billing import Subscription
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.trial_email_campaign import TrialEmailCampaign
from app.models.whatsapp_connection import CONNECTION_STATUS_ACTIVE
from app.schemas.admin_operations import AdminAttentionItem, AdminAttentionResponse

_SUBSCRIPTION_TO_PROFESSIONAL = {
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "cancelled": "canceled",
}


class AdminAttentionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def build(self, permissions: set[str] | None = None) -> AdminAttentionResponse:
        now = datetime.now(UTC)
        items: list[AdminAttentionItem] = []
        if permissions is None or PERMISSION_BILLING_READ in permissions:
            items.extend(await self._billing_divergences())
        if permissions is None or PERMISSION_MESSAGING_READ in permissions:
            items.extend(await self._trial_email_attention(now))
            whatsapp = await self._whatsapp_attention()
            if whatsapp is not None:
                items.append(whatsapp)
        if permissions is None or PERMISSION_PRODUCT_READ in permissions:
            items.extend(await self._stuck_ai_jobs(now))

        items.sort(
            key=lambda item: (
                item.severity == "critical",
                item.created_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        return AdminAttentionResponse(items=items, total=len(items), generated_at=now)

    async def _billing_divergences(self) -> list[AdminAttentionItem]:
        rows = (
            await self.db.execute(
                select(Subscription)
                .options(joinedload(Subscription.professional), joinedload(Subscription.plan))
                .order_by(Subscription.professional_id, Subscription.updated_at.desc())
            )
        ).scalars().unique().all()
        latest_by_professional = {}
        for subscription in rows:
            latest_by_professional.setdefault(subscription.professional_id, subscription)

        items = []
        for subscription in latest_by_professional.values():
            expected = _SUBSCRIPTION_TO_PROFESSIONAL.get(subscription.status)
            professional = subscription.professional
            if expected is None or professional is None or professional.subscription_status == expected:
                continue
            plan_name = subscription.plan.name if subscription.plan else "sem plano"
            items.append(
                AdminAttentionItem(
                    id=f"billing-{subscription.id}",
                    kind="billing_divergence",
                    severity="critical",
                    title=f"Billing divergente: {professional.name}",
                    description=(
                        f"Assinatura {plan_name}: {subscription.status}; "
                        f"acesso local: {professional.subscription_status}."
                    ),
                    target_url=f"/admin/billing?subscriptionId={subscription.id}",
                    professional_id=str(professional.id),
                    created_at=subscription.updated_at,
                )
            )
        return items

    async def _trial_email_attention(self, now: datetime) -> list[AdminAttentionItem]:
        stale_before = now - timedelta(minutes=30)
        rows = (
            await self.db.execute(
                select(TrialEmailCampaign)
                .where(
                    (TrialEmailCampaign.status == "failed")
                    | (
                        (TrialEmailCampaign.status == "processing")
                        & (TrialEmailCampaign.started_at < stale_before)
                    )
                )
                .order_by(TrialEmailCampaign.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        return [
            AdminAttentionItem(
                id=f"trial-email-{campaign.id}",
                kind=(
                    "trial_email_failed" if campaign.status == "failed" else "trial_email_stuck"
                ),
                severity="critical" if campaign.status == "failed" else "warning",
                title=(
                    "Campanha de trial falhou"
                    if campaign.status == "failed"
                    else "Campanha de trial sem progresso"
                ),
                description=(campaign.error or f"Status {campaign.status}; revise antes de reenviar."),
                target_url="/admin/emails",
                created_at=campaign.created_at,
            )
            for campaign in rows
        ]

    async def _stuck_ai_jobs(self, now: datetime) -> list[AdminAttentionItem]:
        stale_before = now - timedelta(minutes=30)
        rows = (
            await self.db.execute(
                select(AIJob)
                .where(
                    AIJob.status.in_(("pending", "processing")),
                    AIJob.created_at < stale_before,
                )
                .order_by(AIJob.created_at.asc())
                .limit(50)
            )
        ).scalars().all()
        return [
            AdminAttentionItem(
                id=f"ai-job-{job.id}",
                kind="ai_job_stuck",
                severity="warning",
                title=f"Job de IA sem progresso: {job.job_type}",
                description=f"Status {job.status} há mais de 30 minutos.",
                target_url="/admin/atencao",
                professional_id=str(job.professional_id),
                created_at=job.created_at,
            )
            for job in rows
        ]

    async def _whatsapp_attention(self) -> AdminAttentionItem | None:
        connection = (
            await self.db.execute(
                select(PlatformWhatsAppConnection)
                .order_by(PlatformWhatsAppConnection.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if connection is not None and connection.status == CONNECTION_STATUS_ACTIVE:
            return None
        return AdminAttentionItem(
            id=f"whatsapp-{connection.id if connection else 'missing'}",
            kind="whatsapp_disconnected",
            severity="warning",
            title="WhatsApp da plataforma desconectado",
            description=(
                connection.last_error
                if connection and connection.last_error
                else "A conexão oficial não está pronta para enviar mensagens."
            ),
            target_url="/admin/whatsapp",
            created_at=connection.updated_at if connection else None,
        )
