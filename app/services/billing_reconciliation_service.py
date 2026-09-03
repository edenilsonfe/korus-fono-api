"""Reconcile subscription state when webhooks are missed."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.billing import get_payment_gateway
from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.billing.stub_gateway import StubPaymentGateway
from app.billing.types import InternalBillingEventType
from app.billing.webhook_normalizer import AsaasWebhookNormalizer, NormalizedBillingEvent
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.plan_change_service import PlanChangeService
from app.services.saas_billing_service import SaasBillingService
from app.services.subscription_payment_method_service import recover_subscription_payment_method

logger = logging.getLogger(__name__)

_ASAAS_SUCCESS = frozenset({"RECEIVED", "CONFIRMED"})


class BillingReconciliationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self._billing = SaasBillingService(db)

    async def reconcile_professional(self, professional_id: str | UUID) -> dict[str, Any]:
        pid = str(professional_id)
        gateway = None
        try:
            gateway = get_payment_gateway()
        except PaymentGatewayConfigError:
            gateway = StubPaymentGateway()

        change_svc = PlanChangeService(self.db, gateway)
        if await change_svc.apply_scheduled_changes(professional_id):
            sub = await self._get_reconcilable_subscription(pid)
            professional = await self.db.get(Professional, UUID(pid))
            return {
                "applied": True,
                "message": "Mudança de plano agendada aplicada com sucesso.",
                "payments_checked": 0,
                "subscription_status": sub.status if sub else None,
                "professional_status": professional.subscription_status if professional else None,
                "plan_slug": sub.plan.slug if sub and sub.plan else None,
            }

        if await change_svc.apply_pending_upgrade(professional_id):
            sub = await self._get_reconcilable_subscription(pid)
            professional = await self.db.get(Professional, UUID(pid))
            return {
                "applied": True,
                "message": "Upgrade de plano aplicado com sucesso.",
                "payments_checked": 1,
                "subscription_status": sub.status if sub else None,
                "professional_status": professional.subscription_status if professional else None,
                "plan_slug": sub.plan.slug if sub and sub.plan else None,
            }

        sub = await self._get_reconcilable_subscription(pid)
        if not sub:
            return {
                "applied": False,
                "message": "Nenhuma assinatura com checkout pendente para reconciliar.",
                "payments_checked": 0,
            }

        provider = (sub.provider or "").lower()
        if provider == "asaas":
            return await self._reconcile_asaas(sub)
        if provider == "stub":
            return await self._reconcile_stub(sub)

        return {
            "applied": False,
            "message": f"Reconciliação não suportada para o provedor '{provider}'.",
            "payments_checked": 0,
        }

    async def simulate_stub_payment(self, professional_id: str | UUID) -> dict[str, Any]:
        """Ativa assinatura stub somente quando o pagamento é simulado explicitamente (dev)."""
        pid = str(professional_id)
        change_svc = PlanChangeService(self.db, StubPaymentGateway())
        if await change_svc.apply_pending_upgrade(professional_id):
            sub = await self._get_reconcilable_subscription(pid)
            professional = await self.db.get(Professional, UUID(pid))
            return {
                "applied": True,
                "message": "Upgrade simulado — plano anual ativado (stub).",
                "payments_checked": 1,
                "subscription_status": "active",
                "professional_status": professional.subscription_status if professional else None,
                "plan_slug": sub.plan.slug if sub and sub.plan else None,
            }

        applied = await self._activate_stub_checkout(pid)
        sub = await self._get_reconcilable_subscription(pid)
        professional = await self.db.get(Professional, UUID(pid))
        if applied:
            return {
                "applied": True,
                "message": "Pagamento simulado — assinatura ativada (stub).",
                "payments_checked": 1,
                "subscription_status": "active",
                "professional_status": professional.subscription_status if professional else None,
                "plan_slug": sub.plan.slug if sub and sub.plan else None,
            }
        if professional and professional.subscription_status == "active":
            return {
                "applied": False,
                "message": "Assinatura já está ativa.",
                "payments_checked": 0,
                "subscription_status": "active",
                "professional_status": professional.subscription_status,
                "plan_slug": sub.plan.slug if sub and sub.plan else None,
            }
        return {
            "applied": False,
            "message": "Nenhum checkout stub pendente para simular.",
            "payments_checked": 0,
        }

    async def _get_reconcilable_subscription(self, professional_id: str) -> Subscription | None:
        pid = UUID(str(professional_id))
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan))
            .where(Subscription.professional_id == pid)
            .order_by(Subscription.updated_at.desc())
        )
        subs = list(result.scalars().unique().all())
        for sub in subs:
            if sub.external_subscription_id or sub.external_checkout_id:
                return sub
        return subs[0] if subs else None

    async def _activate_stub_checkout(self, professional_id: str) -> bool:
        sub = await self._get_reconcilable_subscription(professional_id)
        if not sub or (sub.provider or "") != "stub":
            return False
        ev = NormalizedBillingEvent(
            event_type=InternalBillingEventType.PAYMENT_SUCCEEDED,
            external_event_id=f"stub-reconcile-{sub.external_checkout_id or sub.id}",
            payload={
                "professional_id": professional_id,
                "plan_slug": sub.plan.slug if sub.plan else None,
                "provider": "stub",
                "external_subscription_id": sub.external_subscription_id,
                "external_checkout_id": sub.external_checkout_id,
                "subscription_status": "active",
                "last_payment_at": datetime.now(UTC).isoformat(),
            },
            professional_hint=professional_id,
        )
        row = await self._billing.record_webhook_raw(
            provider="stub",
            external_event_id=ev.external_event_id,
            event_type=ev.event_type.value,
            payload=ev.payload,
            professional_id=professional_id,
        )
        if row:
            await self._billing.apply_normalized_events([ev])
            await self._billing.mark_processed(row.id)
            return True
        return False

    async def _reconcile_stub(self, sub: Subscription) -> dict[str, Any]:
        professional = await self.db.get(Professional, sub.professional_id)
        if sub.status == "active" and professional and professional.subscription_status == "active":
            return {
                "applied": False,
                "message": "Assinatura já está ativa.",
                "payments_checked": 0,
                "subscription_status": sub.status,
                "professional_status": professional.subscription_status,
                "plan_slug": sub.plan.slug if sub.plan else None,
            }
        return {
            "applied": False,
            "message": "Nenhum pagamento confirmado. Use a simulação de pagamento apenas em ambiente de testes.",
            "payments_checked": 0,
            "subscription_status": sub.status,
            "professional_status": professional.subscription_status if professional else None,
            "plan_slug": sub.plan.slug if sub.plan else None,
        }

    async def _reconcile_asaas(self, sub: Subscription) -> dict[str, Any]:
        if not sub.external_subscription_id and not sub.external_checkout_id:
            return {
                "applied": False,
                "message": "Cobrança sem ID externo no Asaas.",
                "payments_checked": 0,
            }

        try:
            gateway = AsaasPaymentGateway()
            if sub.external_subscription_id:
                payments = await gateway.list_subscription_payments(
                    sub.external_subscription_id
                )
                if sub.external_checkout_id and not any(
                    str(payment.get("id")) == str(sub.external_checkout_id)
                    for payment in payments
                ):
                    payments.append(await gateway.get_payment(str(sub.external_checkout_id)))
            else:
                try:
                    payments = [
                        await gateway.get_payment(str(sub.external_checkout_id))
                    ]
                except PaymentGatewayError as exc:
                    if exc.status_code != 404:
                        raise
                    payments = await gateway.list_checkout_payments(
                        str(sub.external_checkout_id)
                    )
        except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
            logger.warning("Asaas reconciliation failed: %s", exc)
            return {"applied": False, "message": str(exc), "payments_checked": 0}

        normalizer = AsaasWebhookNormalizer()
        applied = False
        payment_method_updated = False
        payments_checked = len(payments)
        professional = await self.db.get(Professional, sub.professional_id)
        for payment in payments:
            status = str(payment.get("status", "")).upper()
            if status not in _ASAAS_SUCCESS:
                continue
            if await recover_subscription_payment_method(self.db, sub, payment):
                payment_method_updated = True
            payment = {
                **payment,
                "checkoutSession": payment.get("checkoutSession")
                or sub.external_checkout_id,
                "externalReference": payment.get("externalReference")
                or f"{sub.professional_id}:{sub.plan.slug}",
            }
            events = normalizer.normalize(
                {"event": "PAYMENT_RECEIVED", "payment": payment},
                {},
            )
            for ev in events:
                row = await self._billing.record_webhook_raw(
                    provider="asaas",
                    external_event_id=ev.external_event_id,
                    event_type=ev.event_type.value,
                    payload=ev.payload,
                    professional_id=ev.professional_hint,
                )
                needs_repair = sub.status != "active" or (
                    professional is not None
                    and professional.subscription_status != "active"
                )
                if row or needs_repair:
                    await self._billing.apply_normalized_events(
                        [ev],
                        track_purchase=row is not None,
                    )
                    if row:
                        await self._billing.mark_processed(row.id)
                    applied = True

        if payment_method_updated:
            await self.db.commit()
        professional = await self.db.get(Professional, sub.professional_id)
        await self.db.refresh(sub)
        return {
            "applied": applied or payment_method_updated,
            "message": (
                "Pagamento reconciliado." if applied
                else "Método de pagamento atualizado." if payment_method_updated
                else "Nenhum pagamento confirmado encontrado."
            ),
            "payments_checked": payments_checked,
            "subscription_status": sub.status,
            "professional_status": professional.subscription_status if professional else None,
            "plan_slug": sub.plan.slug if sub.plan else None,
        }
