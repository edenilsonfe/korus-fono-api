"""Plan change orchestration — upgrade proration and scheduled downgrades."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.billing.checkout_urls import build_checkout_return_urls, build_in_app_payment_url
from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.billing_customer_service import BillingCustomerService
from app.services.plan_proration import (
    _add_months,
    calculate_monthly_to_yearly_upgrade,
    infer_period_end,
    is_monthly_interval,
    is_yearly_interval,
)

logger = logging.getLogger(__name__)

_PAYMENT_SUCCESS = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})


def _format_brl(cents: int) -> str:
    return f"R$ {(cents / 100):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d/%m/%Y")


class PlanChangeService:
    def __init__(self, db: AsyncSession, gateway: Any | None = None):
        self.db = db
        self.gateway = gateway

    async def get_active_subscription(self, professional_id: UUID) -> Subscription | None:
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.pending_plan))
            .where(Subscription.professional_id == professional_id)
            .order_by(Subscription.updated_at.desc())
        )
        sub = result.scalars().first()
        if not sub or not sub.plan:
            return None
        if sub.status != "active":
            return None
        return sub

    async def preview_change(
        self, *, professional: Professional, target_plan: Plan
    ) -> dict[str, Any]:
        sub = await self.get_active_subscription(professional.id)
        if not sub or not sub.plan:
            return {"change_type": "new_subscription", "message": "Nova assinatura."}

        current = sub.plan
        if current.slug == target_plan.slug:
            return {"change_type": "none", "message": "Este já é o seu plano atual."}

        if is_monthly_interval(current.billing_interval) and is_yearly_interval(
            target_plan.billing_interval
        ):
            quote = calculate_monthly_to_yearly_upgrade(
                subscription=sub,
                current_plan=current,
                target_plan=target_plan,
            )
            return {
                "change_type": "upgrade",
                "current_plan_slug": current.slug,
                "target_plan_slug": target_plan.slug,
                "credit_cents": quote.credit_cents,
                "charge_cents": quote.charge_cents,
                "target_price_cents": target_plan.price_cents,
                "period_end": quote.period_end.isoformat(),
                "remaining_days": quote.remaining_days,
                "message": (
                    f"Upgrade proporcional: crédito de {_format_brl(quote.credit_cents)} "
                    f"pelos {quote.remaining_days} dia(s) restantes do plano mensal. "
                    f"Cobrança de {_format_brl(quote.charge_cents)}."
                ),
            }

        if is_yearly_interval(current.billing_interval) and is_monthly_interval(
            target_plan.billing_interval
        ):
            effective_at = sub.current_period_end or infer_period_end(sub, current)
            if sub.pending_plan_id and sub.pending_change_at:
                effective_at = sub.pending_change_at
            return {
                "change_type": "downgrade_scheduled",
                "current_plan_slug": current.slug,
                "target_plan_slug": target_plan.slug,
                "scheduled_at": effective_at.isoformat(),
                "message": (
                    f"A mudança para o plano mensal será aplicada automaticamente em "
                    f"{_format_date(effective_at)}, ao fim do período anual contratado."
                ),
            }

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tipo de troca de plano não suportado.",
        )

    async def initiate_change(
        self,
        *,
        professional: Professional,
        subscription: Subscription,
        target_plan: Plan,
        document: str,
        provider: str,
    ) -> dict[str, Any]:
        current = subscription.plan
        if not current:
            raise HTTPException(status_code=400, detail="Assinatura sem plano associado.")
        if current.slug == target_plan.slug:
            raise HTTPException(status_code=400, detail="Você já está neste plano.")

        if is_monthly_interval(current.billing_interval) and is_yearly_interval(
            target_plan.billing_interval
        ):
            return await self._initiate_upgrade(
                professional=professional,
                subscription=subscription,
                current_plan=current,
                target_plan=target_plan,
                document=document,
                provider=provider,
            )

        if is_yearly_interval(current.billing_interval) and is_monthly_interval(
            target_plan.billing_interval
        ):
            return await self._schedule_downgrade(
                subscription=subscription,
                current_plan=current,
                target_plan=target_plan,
                provider=provider,
            )

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tipo de troca de plano não suportado.",
        )

    async def _initiate_upgrade(
        self,
        *,
        professional: Professional,
        subscription: Subscription,
        current_plan: Plan,
        target_plan: Plan,
        document: str,
        provider: str,
    ) -> dict[str, Any]:
        quote = calculate_monthly_to_yearly_upgrade(
            subscription=subscription,
            current_plan=current_plan,
            target_plan=target_plan,
        )

        subscription.pending_plan_id = target_plan.id
        subscription.pending_change_at = None
        subscription.provider = provider
        await self.db.commit()

        if quote.charge_cents <= 0:
            subscription.billing_document = document
            await self._apply_upgrade(subscription, target_plan, provider)
            return {
                "checkout_url": None,
                "session_id": None,
                "status": "completed",
                "provider": provider,
                "change_type": "upgrade",
                "charge_cents": 0,
                "credit_cents": quote.credit_cents,
                "message": "Upgrade aplicado — nenhuma cobrança adicional necessária.",
            }

        if provider == "stub":
            payment_id = f"stub_upgrade_{subscription.id.hex[:12]}"
            subscription.external_checkout_id = payment_id
            subscription.billing_document = document
            await self.db.commit()
            return {
                "checkout_url": build_in_app_payment_url(payment_id),
                "session_id": payment_id,
                "status": "pending",
                "provider": provider,
                "change_type": "upgrade",
                "charge_cents": quote.charge_cents,
                "credit_cents": quote.credit_cents,
                "message": (
                    f"Pague {_format_brl(quote.charge_cents)} (valor proporcional do upgrade) "
                    f"para ativar o plano anual."
                ),
            }

        if not self.gateway:
            raise HTTPException(status_code=503, detail="Gateway de pagamento indisponível.")

        customer_svc = BillingCustomerService(self.db)
        customer_id = await customer_svc.ensure_customer(
            professional_id=str(professional.id),
            provider=provider,
            email=professional.email,
            name=professional.name,
            gateway=self.gateway,
            document=document or None,
        )
        external_ref = f"{professional.id}:{target_plan.slug}:upgrade"
        try:
            if provider == "asaas":
                success_url, cancel_url = build_checkout_return_urls()
                payment = await self.gateway.create_hosted_annual_checkout(
                    customer_id=customer_id,
                    account_id=str(professional.id),
                    plan_slug=target_plan.slug,
                    plan_name=target_plan.name,
                    value_cents=quote.charge_cents,
                    success_url=success_url,
                    cancel_url=cancel_url,
                    external_reference=external_ref,
                )
            else:
                payment = await self.gateway.create_single_payment(
                    customer_id=customer_id,
                    value_cents=quote.charge_cents,
                    description=f"Upgrade {target_plan.name} — KorusFono",
                    external_reference=external_ref,
                )
        except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        payment_id = str(payment.get("payment_id") or payment.get("id"))
        subscription.external_checkout_id = payment_id
        subscription.billing_document = document
        await self.db.commit()

        return {
            "checkout_url": build_in_app_payment_url(payment_id),
            "session_id": payment_id,
            "status": "pending",
            "provider": provider,
            "change_type": "upgrade",
            "charge_cents": quote.charge_cents,
            "credit_cents": quote.credit_cents,
            "message": (
                f"Pague {_format_brl(quote.charge_cents)} (valor proporcional do upgrade) "
                f"para ativar o plano anual."
            ),
        }

    async def _schedule_downgrade(
        self,
        *,
        subscription: Subscription,
        current_plan: Plan,
        target_plan: Plan,
        provider: str,
    ) -> dict[str, Any]:
        effective_at = subscription.current_period_end or infer_period_end(subscription, current_plan)
        subscription.pending_plan_id = target_plan.id
        subscription.pending_change_at = effective_at
        subscription.provider = provider
        await self.db.commit()

        renewal_copy = (
            " Ao fim do acesso anual, será necessário realizar uma nova compra do plano mensal."
            if provider == "asaas" and not subscription.external_subscription_id
            else ""
        )

        return {
            "checkout_url": None,
            "session_id": None,
            "status": "scheduled",
            "provider": provider,
            "change_type": "downgrade_scheduled",
            "scheduled_at": effective_at.isoformat(),
            "message": (
                f"Mudança para o plano mensal agendada para {_format_date(effective_at)}. "
                f"Você mantém o plano anual até lá.{renewal_copy}"
            ),
        }

    async def apply_scheduled_changes(self, professional_id: UUID | str) -> bool:
        pid = UUID(str(professional_id))
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.pending_plan))
            .where(
                Subscription.professional_id == pid,
                Subscription.pending_plan_id.is_not(None),
                Subscription.pending_change_at.is_not(None),
            )
            .order_by(Subscription.updated_at.desc())
        )
        sub = result.scalars().first()
        if not sub or not sub.pending_plan or not sub.pending_change_at:
            return False

        now = datetime.now(UTC)
        effective_at = sub.pending_change_at
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=UTC)
        if now < effective_at:
            return False

        target = sub.pending_plan
        provider = (sub.provider or "stub").lower()

        if provider == "asaas" and not sub.external_subscription_id:
            sub.pending_plan_id = None
            sub.pending_change_at = None
            sub.status = "expired"
            professional = await self.db.get(Professional, sub.professional_id)
            if professional:
                professional.subscription_status = "past_due"
            await self.db.commit()
            return True

        if provider == "asaas" and sub.external_subscription_id and self.gateway:
            try:
                await self.gateway.update_subscription_plan(
                    subscription_id=str(sub.external_subscription_id),
                    value_cents=target.price_cents,
                    cycle="MONTHLY",
                    plan_slug=target.slug,
                    account_id=str(sub.professional_id),
                    next_due_date=effective_at.date().isoformat(),
                )
            except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
                logger.warning("Failed to apply scheduled downgrade on Asaas: %s", exc)
                return False

        sub.plan_id = target.id
        sub.pending_plan_id = None
        sub.pending_change_at = None
        sub.current_period_end = _add_months(effective_at, 1)
        await self.db.commit()
        logger.info(
            "Applied scheduled downgrade professional=%s plan=%s",
            sub.professional_id,
            target.slug,
        )
        return True

    async def apply_pending_upgrade(self, professional_id: UUID | str) -> bool:
        pid = UUID(str(professional_id))
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.pending_plan))
            .where(
                Subscription.professional_id == pid,
                Subscription.pending_plan_id.is_not(None),
                Subscription.pending_change_at.is_(None),
            )
            .order_by(Subscription.updated_at.desc())
        )
        sub = result.scalars().first()
        if not sub or not sub.pending_plan or not sub.external_checkout_id:
            return False

        provider = (sub.provider or "stub").lower()
        if provider == "asaas" and self.gateway:
            try:
                checkout = await self.gateway.get_checkout(
                    str(sub.external_checkout_id)
                )
                if str(checkout.get("status", "")).upper() != "PAID":
                    return False
            except PaymentGatewayError as exc:
                if exc.status_code != 404:
                    return False
                try:
                    payment = await self.gateway.get_payment(
                        str(sub.external_checkout_id)
                    )
                    raw_status = str(payment.get("status", "")).upper()
                    if raw_status not in _PAYMENT_SUCCESS:
                        return False
                except (PaymentGatewayConfigError, PaymentGatewayError):
                    return False
            except PaymentGatewayConfigError:
                return False

        await self._apply_upgrade(sub, sub.pending_plan, provider)
        return True

    async def _apply_upgrade(
        self, subscription: Subscription, target_plan: Plan, provider: str
    ) -> None:
        now = datetime.now(UTC)
        provider_key = (provider or "stub").lower()

        if provider_key == "asaas" and subscription.external_subscription_id and self.gateway:
            try:
                await self.gateway.cancel_subscription(
                    external_subscription_id=str(subscription.external_subscription_id),
                )
            except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
                if not isinstance(exc, PaymentGatewayError) or exc.status_code != 404:
                    logger.warning("Failed to cancel monthly renewal on Asaas: %s", exc)
                    raise
            subscription.external_subscription_id = None

        subscription.plan_id = target_plan.id
        subscription.pending_plan_id = None
        subscription.pending_change_at = None
        subscription.last_payment_at = now
        subscription.current_period_end = _add_months(now, 12)
        subscription.status = "active"
        await self.db.commit()

        professional = await self.db.get(Professional, subscription.professional_id)
        if professional:
            professional.subscription_status = "active"
            await self.db.commit()
