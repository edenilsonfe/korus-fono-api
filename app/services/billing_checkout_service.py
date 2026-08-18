"""In-app checkout session helpers (PIX in-app; cartão via fatura Asaas)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.billing.stub_gateway import StubPaymentGateway
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.plan_proration import (
    calculate_monthly_to_yearly_upgrade,
    is_yearly_interval,
)

_PAYMENT_SUCCESS = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})
_PAYMENT_PENDING = frozenset({"PENDING", "OVERDUE", "AWAITING_RISK_ANALYSIS"})


class BillingCheckoutService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_subscription(
        self, *, session_id: str, professional_id: str
    ) -> Subscription:
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.pending_plan))
            .where(
                Subscription.professional_id == UUID(str(professional_id)),
                Subscription.external_checkout_id == session_id,
            )
            .order_by(Subscription.updated_at.desc())
        )
        sub = result.scalars().first()
        if not sub or not sub.plan:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Sessão de pagamento não encontrada.",
            )
        return sub

    async def get_session(
        self, *, session_id: str, professional: Professional
    ) -> dict[str, Any]:
        sub = await self._get_subscription(
            session_id=session_id, professional_id=str(professional.id)
        )
        display_plan = sub.pending_plan if sub.pending_plan_id and not sub.pending_change_at else sub.plan
        plan = display_plan or sub.plan
        provider = (sub.provider or "stub").lower()
        payment_status = "pending"
        payment_id = sub.external_checkout_id
        charge_cents: int | None = None
        credit_cents: int | None = None
        change_type: str | None = None
        invoice_url: str | None = None

        if sub.pending_plan_id and not sub.pending_change_at and sub.pending_plan and sub.plan:
            change_type = "upgrade"
            try:
                quote = calculate_monthly_to_yearly_upgrade(
                    subscription=sub,
                    current_plan=sub.plan,
                    target_plan=sub.pending_plan,
                )
                credit_cents = quote.credit_cents
                charge_cents = quote.charge_cents
            except ValueError:
                charge_cents = sub.pending_plan.price_cents

        hosted_annual = bool(
            provider == "asaas"
            and is_yearly_interval(plan.billing_interval)
            and (not sub.external_subscription_id or sub.pending_plan_id)
        )

        if provider == "asaas" and payment_id:
            try:
                gateway = AsaasPaymentGateway()
                checkout: dict[str, Any] | None = None
                if hosted_annual:
                    try:
                        checkout = await gateway.get_checkout(str(payment_id))
                    except PaymentGatewayError as exc:
                        if exc.status_code != 404:
                            raise

                if checkout:
                    raw_status = str(checkout.get("status", "")).upper()
                    if raw_status == "PAID":
                        payment_status = "paid"
                    elif raw_status not in {"ACTIVE", "PENDING"}:
                        payment_status = raw_status.lower()
                    for key in ("link", "url", "checkoutUrl"):
                        value = checkout.get(key)
                        if value:
                            invoice_url = str(value)
                            break
                    if not invoice_url:
                        invoice_url = gateway._hosted_checkout_url(checkout)
                else:
                    payment = await gateway.get_payment(str(payment_id))
                    raw_status = str(payment.get("status", "")).upper()
                    if raw_status in _PAYMENT_SUCCESS:
                        payment_status = "paid"
                    elif raw_status not in _PAYMENT_PENDING:
                        payment_status = raw_status.lower()
                    payment_value = payment.get("value")
                    if payment_value is not None:
                        charge_cents = int(round(float(payment_value) * 100))
                    for key in ("invoiceUrl", "bankSlipUrl", "transactionReceiptUrl"):
                        value = payment.get(key)
                        if value:
                            invoice_url = str(value)
                            break
            except (PaymentGatewayConfigError, PaymentGatewayError):
                payment_status = "pending"

        billing_document = "".join(
            char for char in (sub.billing_document or "") if char.isdigit()
        )
        billing_document_type = (
            "cnpj" if len(billing_document) == 14 else "cpf" if len(billing_document) == 11 else None
        )

        return {
            "session_id": session_id,
            "provider": provider,
            "status": payment_status,
            "plan": {
                "slug": plan.slug,
                "name": plan.name,
                "description": plan.description,
                "price_cents": charge_cents if charge_cents is not None else plan.price_cents,
                "currency": plan.currency,
                "billing_interval": plan.billing_interval,
            },
            "customer_name": professional.name,
            "customer_email": professional.email,
            "has_billing_document": bool(billing_document),
            "has_cpf": len(billing_document) == 11,
            "billing_document_type": billing_document_type,
            "charge_cents": charge_cents,
            "credit_cents": credit_cents,
            "change_type": change_type,
            "invoice_url": invoice_url,
        }

    async def generate_pix(
        self, *, session_id: str, professional: Professional
    ) -> dict[str, Any]:
        sub = await self._get_subscription(
            session_id=session_id, professional_id=str(professional.id)
        )
        provider = (sub.provider or "stub").lower()
        payment_id = str(sub.external_checkout_id or session_id)

        if provider == "asaas":
            display_plan = sub.pending_plan if sub.pending_plan_id and not sub.pending_change_at else sub.plan
            if (
                display_plan
                and is_yearly_interval(display_plan.billing_interval)
                and (not sub.external_subscription_id or sub.pending_plan_id)
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "O plano anual usa o checkout seguro do Asaas. "
                        "Escolha PIX ou cartão diretamente na página de pagamento."
                    ),
                )
            try:
                gateway = AsaasPaymentGateway()
                pix = await gateway.get_pix_qr_code(payment_id)
            except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                ) from exc
        else:
            gateway = StubPaymentGateway()
            pix = await gateway.get_pix_qr_code(payment_id)

        return {
            "session_id": session_id,
            "provider": provider,
            "encoded_image": pix.get("encoded_image"),
            "payload": pix.get("payload"),
            "expiration_date": pix.get("expiration_date"),
        }

    async def prepare_card_invoice(
        self, *, session_id: str, professional: Professional
    ) -> dict[str, Any]:
        """Flip cobrança to CREDIT_CARD and return a fresh invoiceUrl for hosted card UI."""
        sub = await self._get_subscription(
            session_id=session_id, professional_id=str(professional.id)
        )
        provider = (sub.provider or "stub").lower()
        payment_id = str(sub.external_checkout_id or session_id)

        if provider != "asaas":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Fatura de cartão disponível apenas com o provedor Asaas.",
            )

        display_plan = sub.pending_plan if sub.pending_plan_id and not sub.pending_change_at else sub.plan
        hosted_annual = bool(
            display_plan
            and is_yearly_interval(display_plan.billing_interval)
            and (not sub.external_subscription_id or sub.pending_plan_id)
        )

        try:
            gateway = AsaasPaymentGateway()
            if hosted_annual:
                return {
                    "session_id": session_id,
                    "invoice_url": gateway._hosted_checkout_url({"id": payment_id}),
                }
            payment = await gateway.ensure_card_billing(payment_id)
        except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

        invoice_url: str | None = None
        for key in ("invoiceUrl", "bankSlipUrl", "transactionReceiptUrl"):
            value = payment.get(key)
            if value:
                invoice_url = str(value)
                break
        if not invoice_url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Asaas não retornou URL da fatura para cartão.",
            )

        return {
            "session_id": session_id,
            "invoice_url": invoice_url,
        }
