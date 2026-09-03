"""Recover missing metadata only for the subscription's current provider charge."""

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.payment_methods import payment_method_from_payload
from app.models.billing import Subscription


async def recover_subscription_payment_method(
    db: AsyncSession,
    subscription: Subscription,
    payment: dict[str, Any],
) -> bool:
    method = payment_method_from_payload(payment)
    checkout_id = subscription.external_checkout_id
    if not method or subscription.payment_method or not checkout_id:
        return False
    provider_references = {
        str(payment.get("id") or ""),
        str(payment.get("checkoutSession") or ""),
    }
    if checkout_id not in provider_references:
        return False

    result = await db.execute(
        update(Subscription)
        .where(
            Subscription.id == subscription.id,
            Subscription.provider == "asaas",
            Subscription.external_checkout_id == checkout_id,
            Subscription.payment_method.is_(None),
        )
        .values(
            payment_method=method,
            # Repairing metadata must not make an old subscription the latest one.
            updated_at=Subscription.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    await db.refresh(subscription, attribute_names=["payment_method"])
    return True
