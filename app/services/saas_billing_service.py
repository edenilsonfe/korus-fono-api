"""Persist webhooks and orchestrate subscription updates."""

import logging
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import NormalizedBillingEvent
from app.models.billing import BillingEvent, Plan, Subscription
from app.models.professional import Professional
from app.services.meta_pixel_service import MetaPixelService

logger = logging.getLogger(__name__)

_SUBSCRIPTION_TO_PROFESSIONAL: dict[str, str] = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "incomplete": "past_due",
    "expired": "trial_expired",
}


def _parse_billing_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, date):
        value = datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _next_period_end(value: datetime, billing_interval: str | None) -> datetime | None:
    interval = (billing_interval or "").lower()
    if interval in ("monthly", "month"):
        return _add_months(value, 1)
    if interval in ("yearly", "annual", "year"):
        return _add_months(value, 12)
    if interval in ("quarterly", "quarter"):
        return _add_months(value, 3)
    return None


class SaasBillingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_webhook_raw(
        self,
        *,
        provider: str,
        external_event_id: str,
        event_type: str,
        payload: dict[str, Any] | None,
        professional_id: str | None = None,
        status: str = "received",
    ) -> BillingEvent | None:
        existing = await self.db.execute(
            select(BillingEvent.id).where(
                BillingEvent.provider == provider,
                BillingEvent.external_event_id == external_event_id,
            )
        )
        if existing.scalar_one_or_none():
            return None
        row = BillingEvent(
            id=uuid.uuid4(),
            provider=provider,
            external_event_id=external_event_id,
            event_type=event_type,
            payload=payload,
            status=status,
            professional_id=UUID(professional_id) if professional_id else None,
            created_at=datetime.now(UTC),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def mark_processed(self, event_id: uuid.UUID) -> None:
        row = (
            await self.db.execute(select(BillingEvent).where(BillingEvent.id == event_id))
        ).scalar_one_or_none()
        if row:
            row.status = "processed"
            row.processed_at = datetime.now(UTC)
            await self.db.commit()

    async def _resolve_professional_id(self, ev: NormalizedBillingEvent) -> str | None:
        if ev.professional_hint:
            return str(ev.professional_hint)
        payload = ev.payload or {}
        for key in ("professional_id", "account_id", "accountId"):
            if payload.get(key):
                return str(payload[key])
        for key in ("external_reference", "externalReference"):
            ref = payload.get(key)
            if ref and ":" in str(ref):
                return str(ref).split(":", 1)[0]
            if ref:
                return str(ref)
        external_sub_id = payload.get("external_subscription_id") or payload.get("subscription_id")
        if external_sub_id:
            result = await self.db.execute(
                select(Subscription.professional_id).where(
                    Subscription.external_subscription_id == str(external_sub_id)
                )
            )
            pid = result.scalar_one_or_none()
            if pid:
                return str(pid)
        return None

    def _target_subscription_status(self, ev: NormalizedBillingEvent) -> str | None:
        payload = ev.payload or {}
        if payload.get("subscription_status"):
            return str(payload["subscription_status"]).lower()

        if ev.event_type in (
            InternalBillingEventType.CHECKOUT_COMPLETED,
            InternalBillingEventType.PAYMENT_SUCCEEDED,
            InternalBillingEventType.SUBSCRIPTION_CREATED,
        ):
            return "active"
        if ev.event_type == InternalBillingEventType.PAYMENT_FAILED:
            return "past_due"
        if ev.event_type == InternalBillingEventType.SUBSCRIPTION_CANCELED:
            return "canceled"
        if ev.event_type == InternalBillingEventType.SUBSCRIPTION_UPDATED:
            mapped = payload.get("subscription_status")
            if mapped:
                return str(mapped).lower()
            return payload.get("status") or payload.get("new_status")
        return None

    async def apply_normalized_events(self, events: list[NormalizedBillingEvent]) -> None:
        for ev in events:
            professional_id = await self._resolve_professional_id(ev)
            if not professional_id:
                logger.info("Skipping billing event %s: no professional_id", ev.external_event_id)
                continue

            sub_status = self._target_subscription_status(ev)
            if not sub_status:
                logger.info(
                    "Skipping billing event %s: unmapped type %s",
                    ev.external_event_id,
                    ev.event_type,
                )
                continue

            sub_status = sub_status.lower()
            payload = ev.payload or {}

            sub_result = await self.db.execute(
                select(Subscription)
                .where(Subscription.professional_id == professional_id)
                .order_by(Subscription.updated_at.desc())
            )
            subscriptions = list(sub_result.scalars().unique().all())
            if not subscriptions:
                continue

            target = subscriptions[0]
            for sub in subscriptions:
                if sub.status in ("active", "trialing", "incomplete"):
                    target = sub
                    break

            target.status = sub_status
            if payload.get("provider"):
                target.provider = str(payload["provider"])
            if payload.get("external_subscription_id"):
                target.external_subscription_id = str(payload["external_subscription_id"])
            if payload.get("external_checkout_id"):
                target.external_checkout_id = str(payload["external_checkout_id"])

            plan_slug = payload.get("plan_slug")
            plan_row = None
            if plan_slug and sub_status == "active":
                plan_row = (
                    await self.db.execute(
                        select(Plan).where(Plan.slug == str(plan_slug), Plan.is_active.is_(True))
                    )
                ).scalar_one_or_none()
                if plan_row:
                    target.plan_id = plan_row.id

            payment_at = _parse_billing_datetime(payload.get("last_payment_at"))
            if payment_at:
                target.last_payment_at = payment_at

            if sub_status == "active" and not target.started_at:
                target.started_at = payment_at or datetime.now(UTC)

            period_end = _parse_billing_datetime(payload.get("current_period_end"))
            if not period_end and payment_at and plan_row:
                period_end = _next_period_end(payment_at, plan_row.billing_interval)
            if period_end:
                target.current_period_end = period_end

            professional = (
                await self.db.execute(
                    select(Professional).where(Professional.id == professional_id)
                )
            ).scalar_one_or_none()
            if professional:
                if sub_status == "active":
                    professional.subscription_status = "active"
                else:
                    professional.subscription_status = _SUBSCRIPTION_TO_PROFESSIONAL.get(
                        sub_status, professional.subscription_status
                    )

            await self.db.commit()
            logger.info(
                "Applied billing event %s -> professional=%s subscription=%s",
                ev.event_type.value,
                professional_id,
                sub_status,
            )

            await self._track_purchase_event(ev, sub_status=sub_status, plan_row=plan_row, professional=professional)

    async def _track_purchase_event(
        self,
        ev: NormalizedBillingEvent,
        *,
        sub_status: str | None,
        plan_row: Plan | None,
        professional: Professional | None,
    ) -> None:
        """Purchase (Meta CAPI) quando um pagamento é confirmado — best-effort."""
        if sub_status != "active" or ev.event_type != InternalBillingEventType.PAYMENT_SUCCEEDED:
            return
        if not professional or not plan_row:
            return
        service = MetaPixelService()
        await service.track_purchase(
            professional_id=str(professional.id),
            email=professional.email,
            name=professional.name,
            value_cents=plan_row.price_cents,
            currency=plan_row.currency,
            plan_slug=plan_row.slug,
            billing_event_id=ev.external_event_id,
        )
