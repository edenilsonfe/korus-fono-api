"""Provider payment facts, independent of current subscription entitlement."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

from app.models.billing import BillingEvent
from app.services.affiliate_credit_service import AffiliateCreditService
from app.services.affiliate_service import AffiliateService


def cents(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return 0
        return int((amount * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def refunded_cents(payload: dict) -> int | None:
    event = payload.get("provider_event")
    if event in {"PAYMENT_REFUNDED", "PAYMENT_CHARGEBACK_REQUESTED"}:
        return None  # Entire reward; do not confuse charge value with partial refund.
    return sum(
        cents(item.get("value"))
        for item in payload.get("refunds", [])
        if isinstance(item, dict) and item.get("status") == "DONE"
    )


def received_at(payload: dict) -> datetime:
    raw = payload.get("received_at")
    try:
        result = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return datetime.now(UTC)


class AffiliateBillingService:
    def __init__(self, db):
        self.db = db

    async def apply(
        self,
        *,
        payload: dict,
        event_id: str,
        professional_id: UUID,
        plan_interval: str,
        reservation_id: str = "",
    ) -> None:
        if (
            payload.get("provider") == "internal_credit"
            and payload.get("provider_event") == "CREDIT_SETTLED"
        ):
            await AffiliateCreditService(self.db).settle_checkout_reservation(
                reservation_id="", payment_id=str(payload.get("id") or "")
            )
            return
        if payload.get("provider") != "asaas":
            return
        payment_id = str(payload.get("id") or "")
        if not payment_id:
            return
        event = payload.get("provider_event")
        rewards = AffiliateService(self.db)
        credit = AffiliateCreditService(self.db)
        reference = str(payload.get("external_reference") or "").split(":")
        if len(reference) == 3 and reference[0] == str(professional_id):
            reservation_id = reference[2]
        if not reservation_id and payload.get("external_checkout_id"):
            from app.models.affiliate import AffiliateCreditCheckout

            reservation_id = (
                await self.db.scalar(
                    select(AffiliateCreditCheckout.reservation_id).where(
                        AffiliateCreditCheckout.source_payment_id
                        == str(payload["external_checkout_id"])
                    )
                )
                or ""
            )
        if event in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            reward = await rewards.record_external_payment(
                referred_professional_id=professional_id,
                external_payment_id=payment_id,
                external_event_id=event_id,
                provider_event=event,
                received_revenue_cents=cents(payload.get("value")),
                plan_interval=plan_interval,
                occurred_at=received_at(payload),
            )
            if event == "PAYMENT_RECEIVED":
                await credit.settle_checkout_reservation(
                    reservation_id=reservation_id, payment_id=payment_id
                )
            # Out-of-order delivery: a known refund must also apply to a reward
            # first created by a delayed payment event.
            if reward or reservation_id:
                previous = (
                    (
                        await self.db.execute(
                            select(BillingEvent).where(
                                BillingEvent.provider == "asaas",
                                BillingEvent.payload["id"].as_string() == payment_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in previous:
                    if (row.payload or {}).get("provider_event") in {
                        "PAYMENT_REFUNDED",
                        "PAYMENT_PARTIALLY_REFUNDED",
                        "PAYMENT_CHARGEBACK_REQUESTED",
                    }:
                        await self._reverse(
                            row.payload,
                            row.external_event_id,
                            payment_id,
                            reservation_id,
                        )
        elif event in {
            "PAYMENT_REFUNDED",
            "PAYMENT_PARTIALLY_REFUNDED",
            "PAYMENT_CHARGEBACK_REQUESTED",
        }:
            await self._reverse(payload, event_id, payment_id, reservation_id)

    async def _reverse(self, payload, event_id, payment_id, reservation_id=""):
        amount = refunded_cents(payload)
        if amount == 0:
            return
        await AffiliateService(self.db).reverse_external_payment(
            external_payment_id=payment_id,
            external_event_id=event_id,
            reversed_revenue_cents=amount,
        )
        charge = cents(payload.get("value"))
        bps = (
            10000
            if amount is None
            else min(10000, amount * 10000 // charge)
            if charge
            else 0
        )
        await AffiliateCreditService(self.db).release_checkout_reservation(
            reservation_id=reservation_id,
            payment_id=payment_id,
            refund_bps=bps,
        )
