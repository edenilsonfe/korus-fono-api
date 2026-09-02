"""Asaas payment gateway — recurring subscriptions with hosted first invoice."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.billing.http_client import request_json
from app.core.config import get_settings

_ASAAS_CYCLE_MAP = {
    "monthly": "MONTHLY",
    "yearly": "YEARLY",
    "weekly": "WEEKLY",
    "quarterly": "QUARTERLY",
    "semiannually": "SEMIANNUALLY",
}

_PAYMENT_PENDING_STATUSES = frozenset({"PENDING", "OVERDUE", "AWAITING_RISK_ANALYSIS"})
_PAYMENT_SUCCESS_STATUSES = frozenset({"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"})

logger = logging.getLogger(__name__)


class AsaasPaymentGateway:
    provider_key = "asaas"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.asaas_api_key:
            raise PaymentGatewayConfigError(
                "ASAAS_API_KEY não configurada. Defina a chave de API do Asaas."
            )
        self._api_key = settings.asaas_api_key
        self._base_url = settings.asaas_api_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "access_token": self._api_key,
            "Content-Type": "application/json",
            "User-Agent": "korus-one-api",
        }

    @staticmethod
    def _next_due_date() -> str:
        return (date.today() + timedelta(days=1)).isoformat()

    @staticmethod
    def _cycle_from_interval(interval: str | None) -> str:
        key = (interval or "monthly").lower().strip()
        return _ASAAS_CYCLE_MAP.get(key, "MONTHLY")

    @staticmethod
    def _digits_only(value: Any) -> str:
        return re.sub(r"\D", "", str(value or ""))

    @staticmethod
    def _payment_checkout_url(payment: dict[str, Any]) -> str:
        for key in ("invoiceUrl", "bankSlipUrl", "transactionReceiptUrl"):
            value = payment.get(key)
            if value:
                return str(value)
        raise PaymentGatewayError(
            "Asaas não retornou URL de pagamento da primeira cobrança da assinatura"
        )

    def _hosted_checkout_url(self, checkout: dict[str, Any]) -> str:
        for key in ("link", "url", "checkoutUrl"):
            value = checkout.get(key)
            if value:
                return str(value)
        checkout_id = checkout.get("id")
        if not checkout_id:
            raise PaymentGatewayError("Asaas não retornou id do checkout")
        host = (
            "https://sandbox.asaas.com"
            if "sandbox" in self._base_url.lower()
            else "https://asaas.com"
        )
        return f"{host}/checkoutSession/show?id={checkout_id}"

    @staticmethod
    def _is_yearly_interval(interval: Any) -> bool:
        return str(interval or "").lower().strip() in {"yearly", "annual", "year"}

    @staticmethod
    def _pick_payment(payments: list[dict[str, Any]]) -> dict[str, Any] | None:
        for payment in payments:
            if str(payment.get("status", "")).upper() in _PAYMENT_SUCCESS_STATUSES:
                return payment
        for payment in payments:
            if str(payment.get("status", "")).upper() in _PAYMENT_PENDING_STATUSES:
                return payment
        return None

    @staticmethod
    def _matches_external_reference(
        payment: dict[str, Any], *, account_id: str, plan_slug: str
    ) -> bool:
        return payment.get("externalReference") == f"{account_id}:{plan_slug}"

    async def create_customer(
        self, *, account_id: str, email: str, name: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name or email,
            "email": email,
            "externalReference": account_id,
        }
        payload.update(self._customer_profile_payload(metadata or {}))
        data = await request_json(
            "POST",
            f"{self._base_url}/customers",
            headers=self._headers(),
            json_body=payload,
        )
        customer_id = data.get("id")
        if not customer_id:
            raise PaymentGatewayError("Asaas não retornou id do cliente")
        return {"external_customer_id": str(customer_id)}

    async def update_customer_document(self, *, customer_id: str, document: str) -> None:
        document_digits = self._digits_only(document)
        if not document_digits:
            return
        await request_json(
            "PUT",
            f"{self._base_url}/customers/{customer_id}",
            headers=self._headers(),
            json_body={"cpfCnpj": document_digits},
        )

    def _customer_profile_payload(self, metadata: dict[str, Any]) -> dict[str, str]:
        mapping = {
            "customer_phone": "phone",
            "customer_address": "address",
            "customer_address_number": "addressNumber",
            "customer_complement": "complement",
            "customer_province": "province",
            "customer_postal_code": "postalCode",
        }
        payload: dict[str, str] = {}
        document = self._digits_only(metadata.get("customer_document"))
        if document:
            payload["cpfCnpj"] = document
        for source, target in mapping.items():
            value = str(metadata.get(source) or "").strip()
            if source in {"customer_phone", "customer_postal_code"}:
                value = self._digits_only(value)
            if value:
                payload[target] = value
        return payload

    async def update_customer_profile(
        self, *, customer_id: str, metadata: dict[str, Any]
    ) -> None:
        payload = self._customer_profile_payload(metadata)
        if not payload:
            return
        await request_json(
            "PUT",
            f"{self._base_url}/customers/{customer_id}",
            headers=self._headers(),
            json_body=payload,
        )

    async def list_subscription_payments(self, subscription_id: str) -> list[dict[str, Any]]:
        data = await request_json(
            "GET",
            f"{self._base_url}/subscriptions/{subscription_id}/payments",
            headers=self._headers(),
        )
        if isinstance(data.get("data"), list):
            return [p for p in data["data"] if isinstance(p, dict)]
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        return []

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        data = await request_json(
            "GET",
            f"{self._base_url}/payments/{payment_id}",
            headers=self._headers(),
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise PaymentGatewayError("Asaas não retornou a cobrança solicitada")
        return data

    async def get_checkout(self, checkout_id: str) -> dict[str, Any]:
        data = await request_json(
            "GET",
            f"{self._base_url}/checkouts/{checkout_id}",
            headers=self._headers(),
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise PaymentGatewayError("Asaas não retornou o checkout solicitado")
        return data

    async def list_checkout_payments(self, checkout_id: str) -> list[dict[str, Any]]:
        query = urlencode({"checkoutSession": checkout_id, "limit": 100})
        data = await request_json(
            "GET",
            f"{self._base_url}/payments?{query}",
            headers=self._headers(),
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [payment for payment in data["data"] if isinstance(payment, dict)]
        if isinstance(data, list):
            return [payment for payment in data if isinstance(payment, dict)]
        return []

    async def list_payments_by_external_reference(
        self, external_reference: str
    ) -> list[dict[str, Any]]:
        query = urlencode({"externalReference": external_reference, "limit": 100})
        data = await request_json(
            "GET",
            f"{self._base_url}/payments?{query}",
            headers=self._headers(),
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [payment for payment in data["data"] if isinstance(payment, dict)]
        return []

    async def list_subscriptions_by_external_reference(
        self, external_reference: str
    ) -> list[dict[str, Any]]:
        query = urlencode({"externalReference": external_reference, "limit": 100})
        data = await request_json(
            "GET",
            f"{self._base_url}/subscriptions?{query}",
            headers=self._headers(),
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [subscription for subscription in data["data"] if isinstance(subscription, dict)]
        return []

    async def create_credit_card_subscription(
        self,
        *,
        customer_id: str,
        account_id: str,
        plan_slug: str,
        plan_name: str,
        value_cents: int,
        checkout_reference: str,
        credit_card: dict[str, str],
        holder_info: dict[str, Any],
        remote_ip: str,
    ) -> dict[str, Any]:
        """Create a recurring card subscription and charge the first cycle today."""
        external_reference = f"{account_id}:{plan_slug}:{checkout_reference}"
        payload = {
            "customer": customer_id,
            "billingType": "CREDIT_CARD",
            "nextDueDate": date.today().isoformat(),
            "value": round(value_cents / 100, 2),
            "cycle": "MONTHLY",
            "description": f"Assinatura {plan_name} — KorusFono",
            "externalReference": external_reference,
            "creditCard": credit_card,
            "creditCardHolderInfo": holder_info,
            "remoteIp": remote_ip,
        }
        subscription = await request_json(
            "POST",
            f"{self._base_url}/subscriptions",
            headers=self._headers(),
            json_body=payload,
            timeout=60.0,
        )
        subscription_id = subscription.get("id")
        if not subscription_id:
            raise PaymentGatewayError("Asaas não retornou id da assinatura com cartão")
        payment = await self._get_first_payment(str(subscription_id), retries=5)
        await self._suspend_until_first_payment(str(subscription_id), payment)
        return {
            "external_subscription_id": str(subscription_id),
            "payment": payment,
        }

    async def create_credit_card_payment(
        self,
        *,
        customer_id: str,
        account_id: str,
        plan_slug: str,
        description: str,
        value_cents: int,
        installments: int,
        checkout_reference: str,
        credit_card: dict[str, str],
        holder_info: dict[str, Any],
        remote_ip: str,
    ) -> dict[str, Any]:
        """Create and immediately process a one-off or installment card charge."""
        payload: dict[str, Any] = {
            "customer": customer_id,
            "billingType": "CREDIT_CARD",
            "dueDate": date.today().isoformat(),
            "description": description,
            "externalReference": f"{account_id}:{plan_slug}:{checkout_reference}",
            "creditCard": credit_card,
            "creditCardHolderInfo": holder_info,
            "remoteIp": remote_ip,
        }
        if installments == 1:
            payload["value"] = round(value_cents / 100, 2)
        else:
            payload["installmentCount"] = installments
            payload["totalValue"] = round(value_cents / 100, 2)

        payment = await request_json(
            "POST",
            f"{self._base_url}/payments",
            headers=self._headers(),
            json_body=payload,
            timeout=60.0,
        )
        payment_id = payment.get("id")
        if not payment_id:
            raise PaymentGatewayError("Asaas não retornou id da cobrança com cartão")
        return {"payment_id": str(payment_id), "payment": payment}

    async def create_pix_payment(
        self,
        *,
        customer_id: str,
        account_id: str,
        plan_slug: str,
        description: str,
        value_cents: int,
        checkout_reference: str,
    ) -> dict[str, Any]:
        """Create a one-off PIX charge for an in-app checkout."""
        payment = await request_json(
            "POST",
            f"{self._base_url}/payments",
            headers=self._headers(),
            json_body={
                "customer": customer_id,
                "billingType": "PIX",
                "dueDate": date.today().isoformat(),
                "value": round(value_cents / 100, 2),
                "description": description,
                "externalReference": (
                    f"{account_id}:{plan_slug}:{checkout_reference}"
                ),
            },
        )
        payment_id = payment.get("id")
        if not payment_id:
            raise PaymentGatewayError("Asaas não retornou id da cobrança PIX")
        return {"payment_id": str(payment_id), "payment": payment}

    async def cancel_checkout(self, checkout_id: str) -> None:
        await request_json(
            "POST",
            f"{self._base_url}/checkouts/{checkout_id}/cancel",
            headers=self._headers(),
        )

    async def delete_payment(self, payment_id: str) -> None:
        await request_json(
            "DELETE",
            f"{self._base_url}/payments/{payment_id}",
            headers=self._headers(),
        )

    async def create_hosted_annual_checkout(
        self,
        *,
        account_id: str,
        plan_slug: str,
        plan_name: str,
        value_cents: int,
        success_url: str,
        cancel_url: str,
        external_reference: str | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "billingTypes": ["PIX", "CREDIT_CARD"],
            "chargeTypes": ["DETACHED", "INSTALLMENT"],
            "installment": {"maxInstallmentCount": 12},
            "minutesToExpire": 1440,
            "callback": {
                "successUrl": success_url,
                "cancelUrl": cancel_url,
                "expiredUrl": cancel_url,
            },
            "items": [
                {
                    "name": plan_name,
                    "description": "Acesso ao KorusFono por 12 meses",
                    "quantity": 1,
                    "value": round(value_cents / 100, 2),
                }
            ],
            "externalReference": external_reference or f"{account_id}:{plan_slug}",
        }
        if customer_id:
            payload["customer"] = customer_id
        data = await request_json(
            "POST",
            f"{self._base_url}/checkouts",
            headers=self._headers(),
            json_body=payload,
        )
        checkout_id = data.get("id")
        if not checkout_id:
            raise PaymentGatewayError("Asaas não retornou id do checkout anual")
        return data

    async def _get_reusable_annual_checkout(
        self,
        *,
        checkout_id: str | None,
        account_id: str,
        plan_slug: str,
    ) -> dict[str, Any] | None:
        if not checkout_id:
            return None
        try:
            checkout = await self.get_checkout(str(checkout_id))
        except PaymentGatewayError as exc:
            if exc.status_code == 404:
                return None
            raise
        expected_ref = f"{account_id}:{plan_slug}"
        external_ref = checkout.get("externalReference")
        if external_ref and external_ref != expected_ref:
            return None
        status = str(checkout.get("status", "")).upper()
        if status in {"ACTIVE", "PENDING", "PAID"}:
            return checkout
        return None

    async def _get_reusable_checkout_payment(
        self,
        *,
        payment_id: str | None,
        account_id: str,
        plan_slug: str,
    ) -> dict[str, Any] | None:
        if not payment_id:
            return None
        payment = await self.get_payment(str(payment_id))
        if not self._matches_external_reference(payment, account_id=account_id, plan_slug=plan_slug):
            return None
        status = str(payment.get("status", "")).upper()
        if status in _PAYMENT_PENDING_STATUSES or status in _PAYMENT_SUCCESS_STATUSES:
            return payment
        return None

    async def _get_first_payment(self, subscription_id: str, *, retries: int = 3) -> dict[str, Any]:
        for attempt in range(retries):
            payments = await self.list_subscription_payments(subscription_id)
            payment = self._pick_payment(payments)
            if payment:
                return payment
            if attempt < retries - 1:
                await asyncio.sleep(0.4)
        raise PaymentGatewayError(
            "Asaas ainda não gerou a primeira cobrança da assinatura. Tente novamente."
        )

    async def _suspend_until_first_payment(
        self,
        subscription_id: str,
        payment: dict[str, Any],
    ) -> None:
        if str(payment.get("status", "")).upper() in _PAYMENT_SUCCESS_STATUSES:
            return
        try:
            await self.suspend_subscription(external_subscription_id=subscription_id)
        except PaymentGatewayError:
            try:
                await self.cancel_subscription(external_subscription_id=subscription_id)
            except PaymentGatewayError:
                logger.exception(
                    "Failed to delete Asaas subscription %s after suspension failure",
                    subscription_id,
                )
            raise

    async def _create_subscription(
        self,
        *,
        customer_id: str,
        account_id: str,
        plan_slug: str,
        price_cents: int,
        plan_name: str,
        billing_interval: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "customer": customer_id,
            "billingType": "UNDEFINED",
            "value": round(price_cents / 100, 2),
            "nextDueDate": self._next_due_date(),
            "cycle": self._cycle_from_interval(billing_interval),
            "description": f"Assinatura {plan_name} — KorusFono",
            "externalReference": f"{account_id}:{plan_slug}",
        }
        data = await request_json(
            "POST",
            f"{self._base_url}/subscriptions",
            headers=self._headers(),
            json_body=payload,
        )
        sub_id = data.get("id")
        if not sub_id:
            raise PaymentGatewayError("Asaas não retornou id da assinatura")
        return data

    async def create_checkout_session(
        self,
        *,
        account_id: str,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = metadata or {}
        price_cents = int(meta.get("charge_cents") or meta.get("price_cents") or 0)
        if price_cents <= 0:
            raise PaymentGatewayError("Valor do plano inválido para checkout Asaas")

        plan_name = str(meta.get("plan_name") or plan_slug)
        billing_interval = meta.get("billing_interval")

        existing_sub_id = meta.get("existing_external_subscription_id")
        if self._is_yearly_interval(billing_interval):
            existing_checkout = None
            if not existing_sub_id:
                existing_checkout = await self._get_reusable_annual_checkout(
                    checkout_id=meta.get("existing_external_checkout_id"),
                    account_id=account_id,
                    plan_slug=(
                        str(meta.get("existing_plan_slug") or plan_slug)
                        if meta.get("replace_existing_checkout")
                        else plan_slug
                    ),
                )
            if existing_checkout:
                checkout_id = str(existing_checkout["id"])
                raw_status = str(existing_checkout.get("status", "")).upper()
                from app.billing.checkout_urls import build_in_app_payment_url

                if meta.get("replace_existing_checkout"):
                    if raw_status == "PAID":
                        return {
                            "external_subscription_id": None,
                            "external_checkout_id": checkout_id,
                            "session_id": checkout_id,
                            "checkout_url": build_in_app_payment_url(checkout_id),
                            "status": "completed",
                            "invoice_url": self._hosted_checkout_url(existing_checkout),
                            "preserve_existing_plan": True,
                        }
                    await self.cancel_checkout(checkout_id)
                    existing_checkout = None

            if existing_checkout:
                checkout_id = str(existing_checkout["id"])
                raw_status = str(existing_checkout.get("status", "")).upper()
                return {
                    "external_subscription_id": None,
                    "external_checkout_id": checkout_id,
                    "session_id": checkout_id,
                    "checkout_url": build_in_app_payment_url(checkout_id),
                    "status": "completed" if raw_status == "PAID" else "pending",
                    "invoice_url": self._hosted_checkout_url(existing_checkout),
                }

            if existing_sub_id:
                existing_payment = await self._get_reusable_checkout_payment(
                    payment_id=meta.get("existing_external_checkout_id"),
                    account_id=account_id,
                    plan_slug=str(meta.get("existing_plan_slug") or plan_slug),
                )
                if (
                    existing_payment
                    and str(existing_payment.get("status", "")).upper()
                    in _PAYMENT_SUCCESS_STATUSES
                ):
                    payment_id = str(existing_payment.get("id") or existing_sub_id)
                    return {
                        "external_subscription_id": str(existing_sub_id),
                        "external_checkout_id": payment_id,
                        "session_id": payment_id,
                        "checkout_url": success_url,
                        "status": "completed",
                        "preserve_existing_plan": bool(
                            meta.get("replace_existing_checkout")
                        ),
                    }
                try:
                    await self.cancel_subscription(
                        external_subscription_id=str(existing_sub_id),
                    )
                except PaymentGatewayError as exc:
                    if exc.status_code != 404:
                        raise

            checkout = await self.create_hosted_annual_checkout(
                account_id=account_id,
                plan_slug=plan_slug,
                plan_name=plan_name,
                value_cents=price_cents,
                success_url=success_url,
                cancel_url=cancel_url,
                customer_id=(
                    str(meta["customer_external_id"])
                    if meta.get("customer_profile_synced") and meta.get("customer_external_id")
                    else None
                ),
            )
            checkout_id = str(checkout["id"])
            from app.billing.checkout_urls import build_in_app_payment_url

            return {
                "external_subscription_id": None,
                "external_checkout_id": checkout_id,
                "session_id": checkout_id,
                "checkout_url": build_in_app_payment_url(checkout_id),
                "status": (
                    "completed"
                    if str(checkout.get("status", "")).upper() == "PAID"
                    else "pending"
                ),
                "invoice_url": self._hosted_checkout_url(checkout),
            }

        if (
            meta.get("replace_existing_checkout")
            and not existing_sub_id
            and self._is_yearly_interval(meta.get("existing_billing_interval"))
        ):
            existing_checkout = await self._get_reusable_annual_checkout(
                checkout_id=meta.get("existing_external_checkout_id"),
                account_id=account_id,
                plan_slug=str(meta.get("existing_plan_slug") or ""),
            )
            if existing_checkout:
                checkout_id = str(existing_checkout["id"])
                raw_status = str(existing_checkout.get("status", "")).upper()
                if raw_status == "PAID":
                    from app.billing.checkout_urls import build_in_app_payment_url

                    return {
                        "external_subscription_id": None,
                        "external_checkout_id": checkout_id,
                        "session_id": checkout_id,
                        "checkout_url": build_in_app_payment_url(checkout_id),
                        "status": "completed",
                        "invoice_url": self._hosted_checkout_url(existing_checkout),
                        "preserve_existing_plan": True,
                    }
                await self.cancel_checkout(checkout_id)

        customer_id = meta.get("customer_external_id")
        customer_document = self._digits_only(meta.get("customer_document"))
        if not customer_id:
            customer = await self.create_customer(
                account_id=account_id,
                email=str(meta.get("customer_email") or ""),
                name=str(meta.get("customer_name") or meta.get("customer_email") or "Cliente"),
                metadata=meta,
            )
            customer_id = customer["external_customer_id"]
        elif customer_document and not meta.get("customer_document_synced"):
            await self.update_customer_document(customer_id=str(customer_id), document=customer_document)

        if existing_sub_id and meta.get("replace_existing_checkout"):
            existing_payment = await self._get_reusable_checkout_payment(
                payment_id=meta.get("existing_external_checkout_id"),
                account_id=account_id,
                plan_slug=str(meta.get("existing_plan_slug") or plan_slug),
            )
            if (
                existing_payment
                and str(existing_payment.get("status", "")).upper()
                in _PAYMENT_SUCCESS_STATUSES
            ):
                payment_id = str(existing_payment.get("id") or existing_sub_id)
                return {
                    "external_subscription_id": str(existing_sub_id),
                    "external_checkout_id": payment_id,
                    "session_id": payment_id,
                    "checkout_url": success_url,
                    "status": "completed",
                    "external_customer_id": str(customer_id),
                    "preserve_existing_plan": True,
                }
            try:
                await self.cancel_subscription(
                    external_subscription_id=str(existing_sub_id),
                )
            except PaymentGatewayError as exc:
                if exc.status_code != 404:
                    raise
            existing_sub_id = None

        if existing_sub_id:
            try:
                await request_json(
                    "POST",
                    f"{self._base_url}/subscriptions/{existing_sub_id}",
                    headers=self._headers(),
                    json_body={
                        "value": round(price_cents / 100, 2),
                        "description": f"Assinatura {plan_name} — KorusFono",
                        "externalReference": f"{account_id}:{plan_slug}",
                        "updatePendingPayments": True,
                    },
                )
            except PaymentGatewayError as exc:
                if exc.status_code != 404:
                    raise
                existing_sub_id = None

        if existing_sub_id:
            subscription_id = str(existing_sub_id)
            payment = await self._get_reusable_checkout_payment(
                payment_id=meta.get("existing_external_checkout_id"),
                account_id=account_id,
                plan_slug=plan_slug,
            )
            if payment and str(payment.get("status", "")).upper() in _PAYMENT_SUCCESS_STATUSES:
                payment_id = str(payment.get("id") or subscription_id)
                return {
                    "external_subscription_id": subscription_id,
                    "external_checkout_id": payment_id,
                    "session_id": payment_id,
                    "checkout_url": success_url,
                    "status": "completed",
                    "external_customer_id": str(customer_id),
                }
            payments = await self.list_subscription_payments(subscription_id)
            payment = payment or self._pick_payment(payments)
            if not payment:
                raise PaymentGatewayError("Nenhuma cobrança pendente encontrada para esta assinatura.")
        else:
            created = await self._create_subscription(
                customer_id=str(customer_id),
                account_id=account_id,
                plan_slug=plan_slug,
                price_cents=price_cents,
                plan_name=plan_name,
                billing_interval=str(billing_interval) if billing_interval else None,
            )
            subscription_id = str(created["id"])
            payment = await self._get_first_payment(subscription_id)

        from app.billing.checkout_urls import build_in_app_payment_url

        payment_id = str(payment.get("id") or subscription_id)
        # Best-effort: redirect back to /planos/retorno after paying on Asaas invoice.
        await self.set_payment_callback(payment_id, success_url=success_url)
        await self._suspend_until_first_payment(subscription_id, payment)

        try:
            invoice_url = self._payment_checkout_url(payment)
        except PaymentGatewayError:
            invoice_url = None

        return {
            "external_subscription_id": subscription_id,
            "external_checkout_id": payment_id,
            "session_id": payment_id,
            "checkout_url": build_in_app_payment_url(payment_id),
            "status": "pending",
            "external_customer_id": str(customer_id),
            "invoice_url": invoice_url,
        }

    async def ensure_pix_billing(self, payment_id: str) -> dict[str, Any]:
        payment = await self.get_payment(payment_id)
        billing_type = str(payment.get("billingType", "")).upper()
        if billing_type != "PIX":
            payment = await request_json(
                "POST",
                f"{self._base_url}/payments/{payment_id}",
                headers=self._headers(),
                json_body={"billingType": "PIX"},
            )
        return payment

    async def ensure_card_billing(self, payment_id: str) -> dict[str, Any]:
        """Force CREDIT_CARD so invoiceUrl shows card form (+ installments when enabled).

        Checkout auto-PIX locks billingType=PIX; without this flip the hosted invoice
        has no card fields.
        """
        payment = await self.get_payment(payment_id)
        billing_type = str(payment.get("billingType", "")).upper()
        if billing_type != "CREDIT_CARD":
            payment = await request_json(
                "POST",
                f"{self._base_url}/payments/{payment_id}",
                headers=self._headers(),
                json_body={"billingType": "CREDIT_CARD"},
            )
        return payment

    async def get_pix_qr_code(self, payment_id: str) -> dict[str, Any]:
        await self.ensure_pix_billing(payment_id)
        last: dict[str, Any] = {}
        for attempt in range(4):
            data = await request_json(
                "GET",
                f"{self._base_url}/payments/{payment_id}/pixQrCode",
                headers=self._headers(),
            )
            last = data
            encoded = data.get("encodedImage") or data.get("encoded_image")
            payload = data.get("payload")
            if encoded and payload:
                break
            if attempt < 3:
                await asyncio.sleep(0.6)
        return {
            "encoded_image": last.get("encodedImage") or last.get("encoded_image"),
            "payload": last.get("payload"),
            "expiration_date": last.get("expirationDate") or last.get("expiration_date"),
        }

    async def set_payment_callback(self, payment_id: str, *, success_url: str) -> None:
        """Attach Asaas invoice return URL. Failures are ignored (PIX still works)."""
        if not success_url:
            return
        try:
            await request_json(
                "POST",
                f"{self._base_url}/payments/{payment_id}",
                headers=self._headers(),
                json_body={
                    "callback": {
                        "successUrl": success_url,
                        "autoRedirect": True,
                    }
                },
            )
        except PaymentGatewayError:
            # ponytail: callback is UX; webhook/reconcile still activate the plan
            return

    async def create_subscription(
        self,
        *,
        account_id: str,
        plan_slug: str,
        customer_external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = metadata or {}
        price_cents = int(meta.get("price_cents") or 0)
        if price_cents <= 0:
            raise PaymentGatewayError("Valor do plano inválido")

        created = await self._create_subscription(
            customer_id=customer_external_id,
            account_id=account_id,
            plan_slug=plan_slug,
            price_cents=price_cents,
            plan_name=str(meta.get("plan_name") or plan_slug),
            billing_interval=meta.get("billing_interval"),
        )
        return {
            "external_subscription_id": str(created["id"]),
            "status": str(created.get("status", "ACTIVE")).lower(),
        }

    async def create_single_payment(
        self,
        *,
        customer_id: str,
        value_cents: int,
        description: str,
        external_reference: str,
    ) -> dict[str, Any]:
        payload = {
            "customer": customer_id,
            "billingType": "UNDEFINED",
            "value": round(value_cents / 100, 2),
            "dueDate": date.today().isoformat(),
            "description": description,
            "externalReference": external_reference,
        }
        data = await request_json(
            "POST",
            f"{self._base_url}/payments",
            headers=self._headers(),
            json_body=payload,
        )
        payment_id = data.get("id")
        if not payment_id:
            raise PaymentGatewayError("Asaas não retornou id da cobrança avulsa")
        return {"payment_id": str(payment_id), "id": str(payment_id)}

    async def update_subscription_plan(
        self,
        *,
        subscription_id: str,
        value_cents: int,
        cycle: str,
        plan_slug: str,
        account_id: str,
        next_due_date: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": round(value_cents / 100, 2),
            "cycle": cycle,
            "externalReference": f"{account_id}:{plan_slug}",
            "updatePendingPayments": False,
        }
        if next_due_date:
            payload["nextDueDate"] = next_due_date
        return await request_json(
            "POST",
            f"{self._base_url}/subscriptions/{subscription_id}",
            headers=self._headers(),
            json_body=payload,
        )

    async def cancel_subscription(self, *, external_subscription_id: str) -> dict[str, Any]:
        data = await request_json(
            "DELETE",
            f"{self._base_url}/subscriptions/{external_subscription_id}",
            headers=self._headers(),
        )
        return {"status": data.get("status", "canceled")}

    async def suspend_subscription(self, *, external_subscription_id: str) -> dict[str, Any]:
        data = await request_json(
            "PUT",
            f"{self._base_url}/subscriptions/{external_subscription_id}",
            headers=self._headers(),
            json_body={"status": "INACTIVE"},
        )
        return {
            "status": str(data.get("status", "inactive")).lower(),
            "external_subscription_id": external_subscription_id,
        }

    async def activate_subscription(
        self,
        *,
        external_subscription_id: str,
        next_due_date: str,
    ) -> dict[str, Any]:
        data = await request_json(
            "PUT",
            f"{self._base_url}/subscriptions/{external_subscription_id}",
            headers=self._headers(),
            json_body={"status": "ACTIVE", "nextDueDate": next_due_date},
        )
        return {
            "status": str(data.get("status", "active")).lower(),
            "external_subscription_id": external_subscription_id,
            "next_due_date": next_due_date,
        }

    async def get_subscription_status(self, *, external_subscription_id: str) -> dict[str, Any]:
        data = await request_json(
            "GET",
            f"{self._base_url}/subscriptions/{external_subscription_id}",
            headers=self._headers(),
        )
        return {
            "status": str(data.get("status", "unknown")).lower(),
            "external_subscription_id": external_subscription_id,
            "external_reference": data.get("externalReference"),
        }
