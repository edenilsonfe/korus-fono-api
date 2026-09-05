"""Bounded recovery of persisted, unprocessed billing events."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import NormalizedBillingEvent
from app.models.affiliate import AffiliatePayoutRequest
from app.models.billing import BillingEvent
from app.services.affiliate_payout_service import AffiliatePayoutService
from app.services.saas_billing_service import SaasBillingService

logger = logging.getLogger(__name__)


async def recover_billing_events(db, *, limit: int = 50) -> int:
    ids = (
        (
            await db.execute(
                select(BillingEvent.id)
                .where(
                    BillingEvent.status == "received",
                    BillingEvent.provider.in_(["asaas", "internal_credit"]),
                )
                .order_by(
                    func.coalesce(BillingEvent.last_attempt_at, BillingEvent.created_at)
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    recovered = 0
    for event_id in ids:
        try:
            row = (
                await db.execute(
                    select(BillingEvent)
                    .where(BillingEvent.id == event_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is None or row.status != "received":
                await db.commit()
                continue
            row.last_attempt_at = datetime.now(UTC)
            payload = row.payload or {}
            service = SaasBillingService(db)
            transfer_event = payload.get("event", "")
            if transfer_event.startswith("TRANSFER_"):
                transfer_id = str((payload.get("transfer") or {}).get("id") or "")
                matched = (
                    await AffiliatePayoutService(db).complete_transfer(
                        provider_transfer_id=transfer_id,
                        succeeded=transfer_event == "TRANSFER_DONE",
                        failure_reason="Transferência não concluída pelo provedor",
                    )
                    if transfer_event
                    in {"TRANSFER_DONE", "TRANSFER_FAILED", "TRANSFER_CANCELLED"}
                    else None
                )
                if matched:
                    await service.mark_processed(row.id)
                    recovered += 1
            else:
                await service.apply_normalized_events(
                    [
                        NormalizedBillingEvent(
                            event_type=InternalBillingEventType(row.event_type),
                            external_event_id=row.external_event_id,
                            payload=payload,
                            professional_hint=str(row.professional_id)
                            if row.professional_id
                            else None,
                        )
                    ]
                )
                if row.status == "processed":
                    recovered += 1
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Billing recovery failed for event %s", event_id)
            await db.execute(
                update(BillingEvent)
                .where(BillingEvent.id == event_id)
                .values(last_attempt_at=datetime.now(UTC))
            )
            await db.commit()
    return recovered


async def reconcile_pending_transfers(db, *, limit: int = 25) -> int:
    # GET only. Never create/retry a transfer automatically.
    ids = (
        (
            await db.execute(
                select(AffiliatePayoutRequest.id)
                .where(
                    AffiliatePayoutRequest.status == "processing",
                    AffiliatePayoutRequest.provider_transfer_id.is_not(None),
                )
                .order_by(AffiliatePayoutRequest.requested_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    completed = 0
    for payout_id in ids:
        try:
            payout = await db.get(AffiliatePayoutRequest, payout_id)
            result = await AffiliatePayoutService(db).reconcile_transfer(
                payout_id=payout_id, provider_transfer_id=payout.provider_transfer_id
            )
            completed += int(result.status in {"paid", "failed"})
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Transfer reconciliation failed for payout %s", payout_id)
    return completed
