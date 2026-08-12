from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.professional import Professional
from app.models.trial_email_campaign import TrialEmailCampaign, TrialEmailDelivery
from app.schemas.admin_trial_email import (
    TrialEmailAudience,
    TrialEmailCampaignResponse,
    TrialEmailPreview,
    TrialEmailRecipientPreview,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.email.resend_client import send_email
from app.services.email.templates import trial_expiration_email


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _campaign_response(campaign: TrialEmailCampaign) -> TrialEmailCampaignResponse:
    return TrialEmailCampaignResponse(
        id=str(campaign.id),
        audience=campaign.audience,
        expires_within_days=campaign.expires_within_days,
        status=campaign.status,
        eligible_count=campaign.eligible_count,
        suppressed_count=campaign.suppressed_count,
        sent_count=campaign.sent_count,
        failed_count=campaign.failed_count,
        skipped_count=campaign.skipped_count,
        error=campaign.error,
        created_at=campaign.created_at,
        started_at=campaign.started_at,
        completed_at=campaign.completed_at,
    )


class TrialEmailCampaignService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    def _base_filters(
        self,
        audience: TrialEmailAudience,
        expires_within_days: int | None,
        now: datetime,
    ) -> list:
        filters = [
            Professional.is_staff.is_(False),
            Professional.is_disabled.is_(False),
            Professional.email_verified_at.is_not(None),
            Professional.trial_ends_at.is_not(None),
        ]
        if audience == "expired":
            filters.extend(
                [
                    Professional.subscription_status.in_(("trialing", "trial_expired")),
                    Professional.trial_ends_at < now,
                ]
            )
        else:
            days = expires_within_days or 3
            filters.extend(
                [
                    Professional.subscription_status == "trialing",
                    Professional.trial_ends_at >= now,
                    Professional.trial_ends_at <= now + timedelta(days=days),
                ]
            )
        return filters

    def _recent_send_exists(self, audience: TrialEmailAudience, now: datetime):
        cutoff = now - timedelta(hours=self.settings.trial_email_resend_cooldown_hours)
        return (
            exists(
                select(TrialEmailDelivery.id)
                .join(
                    TrialEmailCampaign,
                    TrialEmailCampaign.id == TrialEmailDelivery.campaign_id,
                )
                .where(
                    TrialEmailDelivery.professional_id == Professional.id,
                    TrialEmailDelivery.status == "sent",
                    TrialEmailDelivery.sent_at >= cutoff,
                    TrialEmailCampaign.audience == audience,
                )
            )
            .correlate(Professional)
        )

    async def _eligible_professionals(
        self,
        audience: TrialEmailAudience,
        expires_within_days: int | None,
        *,
        sample_limit: int | None = None,
    ) -> tuple[int, int, list[Professional]]:
        now = datetime.now(UTC)
        filters = self._base_filters(audience, expires_within_days, now)
        recent_send = self._recent_send_exists(audience, now)

        base_count = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Professional).where(*filters)
                )
            ).scalar_one()
        )
        eligible_count = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Professional)
                    .where(*filters, ~recent_send)
                )
            ).scalar_one()
        )
        statement = (
            select(Professional)
            .where(*filters, ~recent_send)
            .order_by(Professional.trial_ends_at.asc(), Professional.created_at.asc())
        )
        if sample_limit is not None:
            statement = statement.limit(sample_limit)
        rows = (await self.db.execute(statement)).scalars().all()
        return eligible_count, base_count - eligible_count, list(rows)

    async def preview(
        self,
        audience: TrialEmailAudience,
        expires_within_days: int | None,
    ) -> TrialEmailPreview:
        if audience == "expired":
            expires_within_days = None
        eligible_count, suppressed_count, professionals = await self._eligible_professionals(
            audience, expires_within_days, sample_limit=20
        )
        subject = trial_expiration_email(
            user_name="Profissional",
            audience=audience,
            trial_ends_at="00/00/0000",
            plans_url=self._plans_url(),
        ).subject
        return TrialEmailPreview(
            audience=audience,
            expires_within_days=expires_within_days,
            eligible_count=eligible_count,
            suppressed_count=suppressed_count,
            subject=subject,
            sample=[
                TrialEmailRecipientPreview(
                    id=str(professional.id),
                    name=professional.name,
                    email=professional.email,
                    trial_ends_at=professional.trial_ends_at,
                )
                for professional in professionals
                if professional.trial_ends_at is not None
            ],
        )

    async def create_campaign(
        self,
        *,
        actor: Professional,
        audience: TrialEmailAudience,
        expires_within_days: int | None,
    ) -> TrialEmailCampaignResponse:
        now = datetime.now(UTC)
        if audience == "expired":
            expires_within_days = None
        eligible_count, suppressed_count, professionals = await self._eligible_professionals(
            audience, expires_within_days
        )
        campaign = TrialEmailCampaign(
            actor_id=actor.id,
            audience=audience,
            expires_within_days=expires_within_days,
            status="pending" if eligible_count else "completed",
            eligible_count=eligible_count,
            suppressed_count=suppressed_count,
            completed_at=None if eligible_count else now,
        )
        self.db.add(campaign)
        await self.db.flush()
        self.db.add_all(
            [
                TrialEmailDelivery(
                    campaign_id=campaign.id,
                    professional_id=professional.id,
                    email=professional.email,
                )
                for professional in professionals
            ]
        )
        await AdminAuditService(self.db).log(
            actor_id=actor.id,
            action="create_trial_email_campaign",
            payload={
                "campaign_id": str(campaign.id),
                "audience": audience,
                "expires_within_days": expires_within_days,
                "eligible_count": eligible_count,
                "suppressed_count": suppressed_count,
            },
        )
        await self.db.commit()
        await self.db.refresh(campaign)
        return _campaign_response(campaign)

    async def list_campaigns(self, limit: int = 20) -> list[TrialEmailCampaignResponse]:
        rows = (
            await self.db.execute(
                select(TrialEmailCampaign)
                .order_by(TrialEmailCampaign.created_at.desc())
                .limit(min(max(limit, 1), 100))
            )
        ).scalars().all()
        return [_campaign_response(campaign) for campaign in rows]

    async def process_campaign(self, campaign_id: UUID | str) -> TrialEmailCampaign:
        campaign_uuid = UUID(str(campaign_id))
        campaign = await self.db.get(TrialEmailCampaign, campaign_uuid)
        if campaign is None:
            raise LookupError("Campanha de e-mail não encontrada")
        if campaign.status != "pending":
            return campaign

        campaign.status = "processing"
        campaign.started_at = datetime.now(UTC)
        await self.db.commit()

        deliveries = (
            await self.db.execute(
                select(TrialEmailDelivery)
                .where(
                    TrialEmailDelivery.campaign_id == campaign.id,
                    TrialEmailDelivery.status == "pending",
                )
                .order_by(TrialEmailDelivery.created_at.asc())
            )
        ).scalars().all()

        for delivery in deliveries:
            professional = (
                await self.db.get(Professional, delivery.professional_id)
                if delivery.professional_id is not None
                else None
            )
            now = datetime.now(UTC)
            if not self._still_eligible(professional, campaign, delivery.email, now):
                delivery.status = "skipped"
                delivery.error = "Conta deixou de ser elegível antes do envio."
                await self.db.commit()
                continue

            assert professional is not None and professional.trial_ends_at is not None
            rendered = trial_expiration_email(
                user_name=professional.name,
                audience=campaign.audience,
                trial_ends_at=self._format_trial_date(professional.trial_ends_at),
                plans_url=self._plans_url(),
            )
            try:
                message_id = await asyncio.to_thread(
                    send_email,
                    to_email=delivery.email,
                    subject=rendered.subject,
                    html=rendered.html,
                    text=rendered.text,
                )
                if message_id:
                    delivery.status = "sent"
                    delivery.provider_message_id = message_id
                    delivery.sent_at = now
                else:
                    delivery.status = "skipped"
                    delivery.error = "Envio de e-mail desabilitado ou não configurado."
            except Exception as exc:
                delivery.status = "failed"
                delivery.error = str(exc)[:500]
            await self.db.commit()

        statuses = [delivery.status for delivery in deliveries]
        campaign.sent_count = statuses.count("sent")
        campaign.failed_count = statuses.count("failed")
        campaign.skipped_count = statuses.count("skipped")
        campaign.status = "completed"
        campaign.completed_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(campaign)
        return campaign

    def _still_eligible(
        self,
        professional: Professional | None,
        campaign: TrialEmailCampaign,
        email: str,
        now: datetime,
    ) -> bool:
        if (
            professional is None
            or professional.is_staff
            or professional.is_disabled
            or professional.email_verified_at is None
            or professional.email != email
            or professional.trial_ends_at is None
        ):
            return False
        trial_ends_at = _aware(professional.trial_ends_at)
        if campaign.audience == "expired":
            return (
                professional.subscription_status in ("trialing", "trial_expired")
                and trial_ends_at < now
            )
        return (
            professional.subscription_status == "trialing"
            and now <= trial_ends_at <= now + timedelta(days=campaign.expires_within_days or 3)
        )

    def _format_trial_date(self, value: datetime) -> str:
        try:
            timezone = ZoneInfo(self.settings.clinic_timezone)
        except ZoneInfoNotFoundError:
            timezone = UTC
        return _aware(value).astimezone(timezone).strftime("%d/%m/%Y")

    def _plans_url(self) -> str:
        return f"{self.settings.billing_frontend_base_url}/planos"


async def process_trial_email_campaign(campaign_id: UUID | str) -> None:
    async with AsyncSessionLocal() as db:
        await TrialEmailCampaignService(db).process_campaign(campaign_id)


async def mark_trial_email_campaign_failed(
    campaign_id: UUID | str, error: Exception | str
) -> None:
    async with AsyncSessionLocal() as db:
        campaign = await db.get(TrialEmailCampaign, UUID(str(campaign_id)))
        if campaign is None or campaign.status == "completed":
            return
        campaign.status = "failed"
        campaign.error = str(error)[:500]
        campaign.completed_at = datetime.now(UTC)
        await db.commit()
