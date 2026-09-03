"""Persist webhooks and orchestrate subscription updates."""

import asyncio
import logging
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.payment_methods import payment_method_from_payload
from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import NormalizedBillingEvent
from app.models.billing import BillingEvent, Plan, Subscription
from app.models.professional import Professional
from app.services.email_verification import (
    request_email_verification,
    send_email_verification_email_sync,
)
from app.services.meta_pixel_service import MetaPixelService
from app.services.posthog_analytics_service import PostHogAnalyticsService
from app.services.affiliate_service import AffiliateService

logger = logging.getLogger(__name__)

_SUBSCRIPTION_TO_PROFESSIONAL: dict[str, str] = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "incomplete": "past_due",
    "expired": "trial_expired",
}

_ASAAS_PAID_PAYMENT_STATUSES = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})


def purchase_deduplication_id(ev: NormalizedBillingEvent) -> str:
    """Return one stable purchase id for every notification of the same charge."""
    payload = ev.payload or {}
    provider = str(payload.get("provider") or "billing").strip().lower() or "billing"
    resource_id = next(
        (
            str(payload[key]).strip()
            for key in (
                "id",
                "payment_id",
                "paymentId",
                "checkout_session_id",
                "external_checkout_id",
            )
            if payload.get(key)
        ),
        ev.external_event_id,
    )
    return f"{provider}-{resource_id}"


def _payment_value_cents(payload: dict[str, Any], *, fallback_cents: int) -> int:
    raw = payload.get("value")
    if raw is None:
        return fallback_cents
    try:
        value = (Decimal(str(raw)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return fallback_cents
    return max(0, int(value))


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
        external_checkout_id = (
            payload.get("external_checkout_id")
            or payload.get("checkout_session_id")
            or payload.get("checkoutSession")
        )
        if external_checkout_id:
            result = await self.db.execute(
                select(Subscription.professional_id)
                .where(Subscription.external_checkout_id == str(external_checkout_id))
                .order_by(Subscription.updated_at.desc())
                .limit(1)
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

    async def _cancel_never_paid_asaas_subscription(
        self,
        *,
        target: Subscription,
        payload: dict[str, Any],
    ) -> bool:
        provider = str(payload.get("provider") or target.provider or "").lower()
        external_subscription_id = str(
            payload.get("external_subscription_id")
            or target.external_subscription_id
            or ""
        )
        if provider != "asaas" or not external_subscription_id:
            return False
        if (
            target.status == "active"
            or target.last_payment_at is not None
            or target.current_period_end is not None
        ):
            return False

        gateway = AsaasPaymentGateway()
        payments = await gateway.list_subscription_payments(external_subscription_id)
        if any(
            str(payment.get("status", "")).upper() in _ASAAS_PAID_PAYMENT_STATUSES
            for payment in payments
        ):
            return False

        await gateway.cancel_subscription(
            external_subscription_id=external_subscription_id,
        )
        target.status = "canceled"
        await self.db.commit()
        logger.info(
            "Canceled never-paid Asaas subscription %s after first payment deletion",
            external_subscription_id,
        )
        return True

    async def _activate_first_paid_asaas_subscription(
        self,
        *,
        target: Subscription,
        plan: Plan,
        payment_at: datetime,
        was_never_paid: bool,
    ) -> None:
        provider = str(target.provider or "").lower()
        external_subscription_id = str(target.external_subscription_id or "")
        interval = str(plan.billing_interval or "").lower()
        if (
            provider != "asaas"
            or interval not in {"monthly", "month"}
            or not external_subscription_id
            or not was_never_paid
        ):
            return

        gateway = AsaasPaymentGateway()
        provider_subscription = await gateway.get_subscription_status(
            external_subscription_id=external_subscription_id,
        )
        if str(provider_subscription.get("status", "")).lower() != "inactive":
            return

        await gateway.activate_subscription(
            external_subscription_id=external_subscription_id,
            next_due_date=_add_months(payment_at, 1).date().isoformat(),
        )

    async def apply_normalized_events(
        self,
        events: list[NormalizedBillingEvent],
        *,
        track_purchase: bool = True,
    ) -> None:
        for ev in events:
            professional_id = await self._resolve_professional_id(ev)
            if not professional_id:
                logger.info("Skipping billing event %s: no professional_id", ev.external_event_id)
                continue
            professional_uuid = UUID(str(professional_id))

            payload = ev.payload or {}

            sub_result = await self.db.execute(
                select(Subscription)
                .where(Subscription.professional_id == professional_uuid)
                .order_by(Subscription.updated_at.desc())
            )
            subscriptions = list(sub_result.scalars().unique().all())
            if not subscriptions:
                continue

            external_subscription_id = payload.get("external_subscription_id")
            external_checkout_id = payload.get("external_checkout_id")
            target = next(
                (
                    sub
                    for sub in subscriptions
                    if external_subscription_id
                    and sub.external_subscription_id == str(external_subscription_id)
                ),
                None,
            )
            if target is None:
                target = next(
                    (
                        sub
                        for sub in subscriptions
                        if external_checkout_id
                        and sub.external_checkout_id == str(external_checkout_id)
                    ),
                    None,
                )
            if target is None and (external_subscription_id or external_checkout_id):
                logger.info(
                    "Skipping stale billing event %s: provider resource is no longer current",
                    ev.external_event_id,
                )
                continue
            if target is None:
                target = subscriptions[0]
                for sub in subscriptions:
                    if sub.status in ("active", "trialing", "incomplete"):
                        target = sub
                        break

            if ev.event_type == InternalBillingEventType.PAYMENT_DELETED:
                await self._cancel_never_paid_asaas_subscription(
                    target=target,
                    payload=payload,
                )
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

            was_never_paid = (
                target.last_payment_at is None
                and target.current_period_end is None
            )

            target.status = sub_status
            if payload.get("provider"):
                target.provider = str(payload["provider"])
            if payload.get("external_subscription_id"):
                target.external_subscription_id = str(payload["external_subscription_id"])
            if payload.get("external_checkout_id"):
                target.external_checkout_id = str(payload["external_checkout_id"])
            payment_method = payment_method_from_payload(payload)
            if payment_method:
                target.payment_method = payment_method

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
            elif sub_status == "active" and target.plan_id:
                plan_row = await self.db.get(Plan, target.plan_id)

            payment_at = _parse_billing_datetime(payload.get("last_payment_at"))
            if (
                ev.event_type == InternalBillingEventType.PAYMENT_SUCCEEDED
                and plan_row
            ):
                await self._activate_first_paid_asaas_subscription(
                    target=target,
                    plan=plan_row,
                    payment_at=payment_at or datetime.now(UTC),
                    was_never_paid=was_never_paid,
                )
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
                    select(Professional)
                    .where(Professional.id == professional_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            should_send_verification = False
            if professional:
                if sub_status == "active":
                    professional.subscription_status = "active"
                    if ev.event_type == InternalBillingEventType.PAYMENT_SUCCEEDED:
                        should_send_verification = (
                            professional.signup_payment_required
                            and professional.email_verified_at is None
                        )
                        professional.signup_payment_required = False
                else:
                    professional.subscription_status = _SUBSCRIPTION_TO_PROFESSIONAL.get(
                        sub_status, professional.subscription_status
                    )

            provider_event = str(payload.get("provider_event") or "")
            source_payment_id = str(
                payload.get("id")
                or payload.get("payment_id")
                or purchase_deduplication_id(ev)
            )
            if ev.event_type == InternalBillingEventType.PAYMENT_SUCCEEDED and plan_row:
                fallback_revenue_cents = (
                    target.checkout_charge_cents
                    if target.checkout_charge_cents is not None
                    else plan_row.price_cents
                )
                external_revenue_cents = _payment_value_cents(
                    payload,
                    fallback_cents=fallback_revenue_cents,
                )
                await AffiliateService(self.db).record_external_payment(
                    referred_professional_id=professional_uuid,
                    external_payment_id=source_payment_id,
                    external_event_id=ev.external_event_id,
                    provider_event=provider_event or "PAYMENT_CONFIRMED",
                    received_revenue_cents=external_revenue_cents,
                    plan_interval=plan_row.billing_interval,
                    occurred_at=payment_at or datetime.now(UTC),
                )
            elif ev.event_type in {
                InternalBillingEventType.PAYMENT_FAILED,
                InternalBillingEventType.PAYMENT_DELETED,
            }:
                raw_reversal = payload.get("value") or payload.get("refundedValue")
                reversed_cents = None
                if raw_reversal is not None:
                    try:
                        reversed_cents = int(round(float(raw_reversal) * 100))
                    except (TypeError, ValueError):
                        reversed_cents = None
                await AffiliateService(self.db).reverse_external_payment(
                    external_payment_id=source_payment_id,
                    external_event_id=ev.external_event_id,
                    reversed_revenue_cents=reversed_cents,
                )
                if target.checkout_session_id is not None:
                    from app.services.affiliate_credit_service import AffiliateCreditService

                    await AffiliateCreditService(self.db).release_checkout_reservation(
                        reservation_id=str(target.checkout_session_id)
                    )

            if (
                provider_event == "PAYMENT_RECEIVED"
                and target.checkout_session_id is not None
            ):
                from app.services.affiliate_credit_service import AffiliateCreditService

                await AffiliateCreditService(self.db).settle_checkout_reservation(
                    reservation_id=str(target.checkout_session_id)
                )

            await self.db.commit()
            logger.info(
                "Applied billing event %s -> professional=%s subscription=%s",
                ev.event_type.value,
                professional_id,
                sub_status,
            )

            if should_send_verification and professional:
                await self._send_signup_verification_email(professional)

            if track_purchase:
                await self._track_purchase_event(
                    ev,
                    sub_status=sub_status,
                    plan_row=plan_row,
                    professional=professional,
                    subscription=target,
                )

    async def _send_signup_verification_email(self, professional: Professional) -> None:
        """Send the verification link only after a paid signup is unlocked."""
        try:
            raw_token = await request_email_verification(
                self.db,
                professional,
                force=True,
            )
            if raw_token is not None:
                await asyncio.to_thread(
                    send_email_verification_email_sync,
                    professional.email,
                    professional.name,
                    raw_token,
                )
        except Exception:
            # Payment activation must remain authoritative even when email delivery fails.
            logger.exception(
                "Failed to queue post-payment email verification for professional %s",
                professional.id,
            )

    async def _track_purchase_event(
        self,
        ev: NormalizedBillingEvent,
        *,
        sub_status: str | None,
        plan_row: Plan | None,
        professional: Professional | None,
        subscription: Subscription,
    ) -> None:
        """Purchase server-side quando um pagamento é confirmado — best-effort."""
        if sub_status != "active" or ev.event_type != InternalBillingEventType.PAYMENT_SUCCEEDED:
            return
        if not professional or not plan_row:
            return
        value_cents = (
            subscription.checkout_charge_cents
            if subscription.checkout_charge_cents is not None
            else plan_row.price_cents
        )
        purchase_id = purchase_deduplication_id(ev)
        await MetaPixelService().track_purchase(
            professional_id=str(professional.id),
            email=professional.email,
            name=professional.name,
            value_cents=value_cents,
            currency=plan_row.currency,
            plan_slug=plan_row.slug,
            billing_event_id=purchase_id,
        )
        await PostHogAnalyticsService().track_purchase(
            professional_id=str(professional.id),
            plan_slug=plan_row.slug,
            value_cents=value_cents,
            currency=plan_row.currency,
            billing_event_id=purchase_id,
            session_id=(
                str(subscription.checkout_session_id)
                if subscription.checkout_session_id is not None
                else None
            ),
        )
