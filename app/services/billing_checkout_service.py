"""In-app checkout helpers for PIX and transient card processing."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.billing.stub_gateway import StubPaymentGateway
from app.models.billing import Subscription
from app.models.professional import Professional
from app.schemas.billing import CreditCardPaymentRequest
from app.services.billing_customer_service import BillingCustomerService
from app.services.billing_profile_service import asaas_customer_profile
from app.services.plan_proration import (
    calculate_monthly_to_yearly_upgrade,
    is_yearly_interval,
)
from app.services.subscription_payment_method_service import (
    recover_subscription_payment_method,
)

_PAYMENT_SUCCESS = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})
_PAYMENT_PENDING = frozenset({"PENDING", "OVERDUE", "AWAITING_RISK_ANALYSIS"})

logger = logging.getLogger(__name__)


class BillingCheckoutService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_subscription(
        self, *, session_id: str, professional_id: str, lock: bool = False
    ) -> Subscription:
        session_filters = [Subscription.external_checkout_id == session_id]
        try:
            local_session_id = UUID(str(session_id))
        except ValueError:
            local_session_id = None
        if local_session_id:
            session_filters.extend(
                [
                    Subscription.checkout_session_id == local_session_id,
                    Subscription.id == local_session_id,
                ]
            )
        statement = (
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.pending_plan))
            .where(
                Subscription.professional_id == UUID(str(professional_id)),
                or_(*session_filters),
            )
            .order_by(Subscription.updated_at.desc())
        )
        if lock:
            statement = statement.with_for_update(of=Subscription)
        result = await self.db.execute(statement)
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
                    if await recover_subscription_payment_method(self.db, sub, payment):
                        await self.db.commit()
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

        if provider == "stub" and sub.status == "active":
            payment_status = "paid"

        if payment_status == "paid" and professional.signup_payment_required:
            from app.services.billing_reconciliation_service import (
                BillingReconciliationService,
            )

            await BillingReconciliationService(self.db).reconcile_professional(
                professional.id
            )
            await self.db.refresh(professional)
            await self.db.refresh(sub)

        billing_document = "".join(
            char for char in (sub.billing_document or "") if char.isdigit()
        )
        billing_document_type = (
            "cnpj" if len(billing_document) == 14 else "cpf" if len(billing_document) == 11 else None
        )

        if sub.checkout_charge_cents is not None:
            charge_cents = sub.checkout_charge_cents

        return {
            "session_id": str(sub.checkout_session_id or session_id),
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
            "billing_document": billing_document,
            "billing_postal_code": professional.billing_postal_code,
            "billing_address_number": professional.billing_address_number,
            "billing_address_complement": professional.billing_address_complement,
            "billing_phone": professional.phone,
            "charge_cents": charge_cents,
            "credit_cents": credit_cents,
            "change_type": change_type,
            "invoice_url": invoice_url,
            "access_granted": (
                payment_status == "paid"
                and professional.subscription_status == "active"
                and not professional.signup_payment_required
            ),
        }

    async def pay_credit_card(
        self,
        *,
        session_id: str,
        professional: Professional,
        payload: CreditCardPaymentRequest,
        remote_ip: str,
    ) -> dict[str, Any]:
        """Process a card payment while keeping PAN/CVV transient in memory only."""
        sub = await self._get_subscription(
            session_id=session_id,
            professional_id=str(professional.id),
            lock=True,
        )
        provider = (sub.provider or "stub").lower()
        if provider != "asaas":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pagamento transparente disponível apenas com o provedor Asaas.",
            )

        plan = sub.pending_plan if sub.pending_plan_id and not sub.pending_change_at else sub.plan
        if not plan:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Plano da sessão de pagamento não encontrado.",
            )
        yearly = is_yearly_interval(plan.billing_interval)
        if not yearly and payload.installments != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="O plano mensal deve ser pago em uma única parcela.",
            )

        billing_document = "".join(
            character for character in (sub.billing_document or "") if character.isdigit()
        )
        if not billing_document:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Informe um CPF ou CNPJ de cobrança antes de pagar com cartão.",
            )

        try:
            gateway = AsaasPaymentGateway()
            customer_id = await BillingCustomerService(self.db).ensure_customer(
                professional_id=str(professional.id),
                provider="asaas",
                email=professional.email,
                name=professional.name,
                gateway=gateway,
                document=billing_document,
                profile=asaas_customer_profile(professional),
            )
            credit_card = {
                "holderName": payload.holder_name,
                "number": payload.number.get_secret_value(),
                "expiryMonth": payload.expiry_month,
                "expiryYear": payload.expiry_year,
                "ccv": payload.ccv.get_secret_value(),
            }
            holder_info = {
                "name": payload.holder_name,
                "email": str(payload.holder_email),
                "cpfCnpj": payload.holder_document,
                "postalCode": payload.postal_code,
                "addressNumber": payload.address_number,
                "addressComplement": payload.address_complement,
                "phone": payload.phone,
                "mobilePhone": payload.phone,
            }
            checkout_reference = str(sub.checkout_session_id or sub.id)
            charge_cents = sub.checkout_charge_cents or plan.price_cents
            old_subscription_id = sub.external_subscription_id
            old_checkout_id = sub.external_checkout_id
            old_checkout_is_payment = False
            external_reference = (
                f"{professional.id}:{plan.slug}:{checkout_reference}"
            )

            if yearly:
                existing_payments = await gateway.list_payments_by_external_reference(
                    external_reference
                )
                old_checkout_is_payment = any(
                    str(candidate.get("id")) == str(old_checkout_id)
                    for candidate in existing_payments
                )
                payment = self._pick_card_attempt(existing_payments)
                if payment:
                    payment_id = str(payment["id"])
                else:
                    result = await gateway.create_credit_card_payment(
                        customer_id=customer_id,
                        account_id=str(professional.id),
                        plan_slug=plan.slug,
                        description=f"{plan.name} — acesso por 12 meses",
                        value_cents=charge_cents,
                        installments=payload.installments,
                        checkout_reference=checkout_reference,
                        credit_card=credit_card,
                        holder_info=holder_info,
                        remote_ip=remote_ip,
                    )
                    payment = result["payment"]
                    payment_id = str(result["payment_id"])
                sub.external_checkout_id = payment_id
            else:
                existing_subscriptions = (
                    await gateway.list_subscriptions_by_external_reference(
                        external_reference
                    )
                )
                existing_subscription = next(
                    (
                        candidate
                        for candidate in existing_subscriptions
                        if str(candidate.get("status", "")).upper() == "ACTIVE"
                        and candidate.get("id")
                    ),
                    None,
                )
                payment = None
                if existing_subscription:
                    recovered_subscription_id = str(existing_subscription["id"])
                    payment = self._pick_card_attempt(
                        await gateway.list_subscription_payments(
                            recovered_subscription_id
                        )
                    )
                    if not payment:
                        raise PaymentGatewayError(
                            "Assinatura localizada, mas a primeira cobrança ainda não está disponível"
                        )
                if existing_subscription and payment:
                    new_subscription_id = str(existing_subscription["id"])
                else:
                    result = await gateway.create_credit_card_subscription(
                        customer_id=customer_id,
                        account_id=str(professional.id),
                        plan_slug=plan.slug,
                        plan_name=plan.name,
                        value_cents=charge_cents,
                        checkout_reference=checkout_reference,
                        credit_card=credit_card,
                        holder_info=holder_info,
                        remote_ip=remote_ip,
                    )
                    payment = result["payment"]
                    new_subscription_id = str(result["external_subscription_id"])
                sub.external_subscription_id = new_subscription_id
                sub.external_checkout_id = str(payment["id"])

            sub.payment_method = "credit_card"
            await self.db.commit()
        except PaymentGatewayError as exc:
            logger.warning(
                "Asaas transparent card payment failed professional_id=%s status_code=%s",
                professional.id,
                exc.status_code,
            )
            if exc.status_code == 400:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        "Pagamento não autorizado. Confira os dados, tente outro cartão "
                        "ou escolha PIX."
                    ),
                ) from None
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Não foi possível confirmar o pagamento. Aguarde alguns instantes e "
                    "verifique o status antes de tentar novamente."
                ),
            ) from None

        # Provider cleanup happens only after the new charge exists. Failures here must not
        # turn a successful card authorization into an apparent client-side failure.
        try:
            if yearly and old_checkout_id and old_checkout_id != sub.external_checkout_id:
                if old_checkout_is_payment:
                    await gateway.delete_payment(str(old_checkout_id))
                else:
                    await gateway.cancel_checkout(str(old_checkout_id))
            elif (
                not yearly
                and old_subscription_id
                and old_subscription_id != sub.external_subscription_id
            ):
                await gateway.cancel_subscription(
                    external_subscription_id=str(old_subscription_id)
                )
        except PaymentGatewayError as exc:
            logger.warning(
                "Asaas superseded checkout cleanup failed professional_id=%s status_code=%s",
                professional.id,
                exc.status_code,
            )

        raw_status = str(payment.get("status", "")).upper()
        if sub.external_subscription_id and sub.checkout_recurring_price_cents:
            try:
                await gateway.set_recurring_price(external_subscription_id=sub.external_subscription_id,
                    value_cents=sub.checkout_recurring_price_cents)
            except PaymentGatewayError:
                logger.exception("Future affiliate price reconciliation pending for subscription %s", sub.id)
        payment_status = "paid" if raw_status in _PAYMENT_SUCCESS else "pending"
        if payment_status == "paid":
            from app.services.billing_reconciliation_service import (
                BillingReconciliationService,
            )

            await BillingReconciliationService(self.db).reconcile_professional(
                professional.id
            )

        return {
            "session_id": str(sub.checkout_session_id or sub.id),
            "provider": provider,
            "status": payment_status,
            "message": (
                "Pagamento confirmado."
                if payment_status == "paid"
                else "Pagamento recebido e em processamento."
            ),
        }

    @staticmethod
    def _pick_card_attempt(payments: list[dict[str, Any]]) -> dict[str, Any] | None:
        for accepted_statuses in (_PAYMENT_SUCCESS, _PAYMENT_PENDING):
            for payment in payments:
                if (
                    payment.get("id")
                    and str(payment.get("billingType", "")).upper()
                    in {"", "CREDIT_CARD"}
                    and str(payment.get("status", "")).upper() in accepted_statuses
                ):
                    return payment
        return None

    async def generate_pix(
        self, *, session_id: str, professional: Professional
    ) -> dict[str, Any]:
        sub = await self._get_subscription(
            session_id=session_id,
            professional_id=str(professional.id),
            lock=True,
        )
        provider = (sub.provider or "stub").lower()
        payment_id = str(sub.external_checkout_id or session_id)

        if provider == "asaas":
            display_plan = sub.pending_plan if sub.pending_plan_id and not sub.pending_change_at else sub.plan
            transparent_annual = bool(
                display_plan
                and is_yearly_interval(display_plan.billing_interval)
                and (not sub.external_subscription_id or sub.pending_plan_id)
            )
            try:
                gateway = AsaasPaymentGateway()
                if transparent_annual and display_plan:
                    billing_document = "".join(
                        character
                        for character in (sub.billing_document or "")
                        if character.isdigit()
                    )
                    if not billing_document:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=(
                                "Informe um CPF ou CNPJ de cobrança antes de gerar o PIX."
                            ),
                        )

                    customer_id = await BillingCustomerService(self.db).ensure_customer(
                        professional_id=str(professional.id),
                        provider="asaas",
                        email=professional.email,
                        name=professional.name,
                        gateway=gateway,
                        document=billing_document,
                        profile=asaas_customer_profile(professional),
                    )
                    checkout_reference = str(sub.checkout_session_id or sub.id)
                    external_reference = (
                        f"{professional.id}:{display_plan.slug}:{checkout_reference}"
                    )
                    payments = await gateway.list_payments_by_external_reference(
                        external_reference
                    )
                    payment = self._pick_pix_attempt(payments)
                    old_checkout_id = sub.external_checkout_id
                    old_checkout_is_payment = any(
                        str(candidate.get("id")) == str(old_checkout_id)
                        for candidate in payments
                    )
                    if payment:
                        payment_id = str(payment["id"])
                    else:
                        result = await gateway.create_pix_payment(
                            customer_id=customer_id,
                            account_id=str(professional.id),
                            plan_slug=display_plan.slug,
                            description=f"{display_plan.name} — acesso por 12 meses",
                            value_cents=(
                                sub.checkout_charge_cents or display_plan.price_cents
                            ),
                            checkout_reference=checkout_reference,
                        )
                        payment_id = str(result["payment_id"])

                    sub.external_checkout_id = payment_id
                    await self.db.commit()
                    pix = await gateway.get_pix_qr_code(payment_id)

                    if old_checkout_id and str(old_checkout_id) != payment_id:
                        try:
                            if old_checkout_is_payment:
                                await gateway.delete_payment(str(old_checkout_id))
                            else:
                                await gateway.cancel_checkout(str(old_checkout_id))
                        except PaymentGatewayError as exc:
                            logger.warning(
                                "Asaas superseded annual checkout cleanup failed "
                                "professional_id=%s status_code=%s",
                                professional.id,
                                exc.status_code,
                            )
                else:
                    pix = await gateway.get_pix_qr_code(payment_id)
            except (PaymentGatewayConfigError, PaymentGatewayError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=str(exc),
                ) from exc
        else:
            gateway = StubPaymentGateway()
            pix = await gateway.get_pix_qr_code(payment_id)

        sub.payment_method = "pix"
        await self.db.commit()

        return {
            "session_id": session_id,
            "provider": provider,
            "encoded_image": pix.get("encoded_image"),
            "payload": pix.get("payload"),
            "expiration_date": pix.get("expiration_date"),
        }

    @staticmethod
    def _pick_pix_attempt(payments: list[dict[str, Any]]) -> dict[str, Any] | None:
        for accepted_statuses in (_PAYMENT_SUCCESS, _PAYMENT_PENDING):
            for payment in payments:
                if (
                    payment.get("id")
                    and str(payment.get("billingType", "")).upper() == "PIX"
                    and str(payment.get("status", "")).upper() in accepted_statuses
                ):
                    return payment
        return None

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

        sub.payment_method = "credit_card"
        await self.db.commit()

        return {
            "session_id": session_id,
            "invoice_url": invoice_url,
        }
