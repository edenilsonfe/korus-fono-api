"""Trial / subscription write access."""

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.plan_proration import is_yearly_interval


class EntitlementService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def can_write(self, professional: Professional) -> bool:
        now = datetime.now(UTC)

        if professional.subscription_status == "trialing":
            if professional.trial_ends_at and now > professional.trial_ends_at:
                professional.subscription_status = "trial_expired"
                await self.db.commit()
                return False
            return True

        if professional.subscription_status in ("trial_expired", "canceled", "past_due"):
            return False

        if professional.subscription_status == "active":
            result = await self.db.execute(
                select(Subscription)
                .options(joinedload(Subscription.plan))
                .where(Subscription.professional_id == professional.id)
                .order_by(Subscription.updated_at.desc())
            )
            subscription = result.scalars().first()
            if (
                subscription
                and subscription.plan
                and (subscription.provider or "").lower() == "asaas"
                and not subscription.external_subscription_id
                and is_yearly_interval(subscription.plan.billing_interval)
                and subscription.current_period_end
            ):
                period_end = subscription.current_period_end
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=UTC)
                if now >= period_end:
                    subscription.status = "expired"
                    professional.subscription_status = "past_due"
                    await self.db.commit()
                    return False
            return True

        return False

    async def ensure_write_allowed(self, professional: Professional) -> None:
        if not await self.can_write(professional):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Assinatura ou período de teste indisponível. Renove ou assine um plano para continuar.",
            )
