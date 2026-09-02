"""Stub gateway for tests and environments without a real PSP."""

import uuid
from typing import Any

from app.billing.checkout_urls import build_in_app_payment_url


class StubPaymentGateway:
    provider_key = "stub"

    async def create_customer(
        self, *, account_id: str, email: str, name: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"external_customer_id": f"stub_cust_{uuid.uuid4().hex[:12]}"}

    async def create_checkout_session(
        self,
        *,
        account_id: str,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payment_id = f"stub_pay_{uuid.uuid4().hex[:12]}"
        subscription_id = f"stub_sub_{uuid.uuid4().hex[:12]}"
        return {
            "external_subscription_id": subscription_id,
            "external_checkout_id": payment_id,
            "session_id": payment_id,
            "checkout_url": build_in_app_payment_url(payment_id),
            "status": "pending",
        }

    async def update_customer_document(self, *, customer_id: str, document: str) -> None:
        return None

    async def update_customer_profile(
        self, *, customer_id: str, metadata: dict[str, Any]
    ) -> None:
        return None

    async def get_pix_qr_code(self, payment_id: str) -> dict[str, Any]:
        return {
            "encoded_image": None,
            "payload": f"00020126STUBPIX{payment_id[-12:].upper()}",
            "expiration_date": None,
        }

    async def create_subscription(
        self,
        *,
        account_id: str,
        plan_slug: str,
        customer_external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "external_subscription_id": f"stub_sub_{uuid.uuid4().hex[:12]}",
            "status": "active",
        }

    async def cancel_subscription(self, *, external_subscription_id: str) -> dict[str, Any]:
        return {"status": "canceled"}

    async def suspend_subscription(self, *, external_subscription_id: str) -> dict[str, Any]:
        return {"status": "inactive", "external_subscription_id": external_subscription_id}

    async def activate_subscription(
        self, *, external_subscription_id: str, next_due_date: str
    ) -> dict[str, Any]:
        return {
            "status": "active",
            "external_subscription_id": external_subscription_id,
            "next_due_date": next_due_date,
        }

    async def get_subscription_status(self, *, external_subscription_id: str) -> dict[str, Any]:
        return {"status": "active", "external_subscription_id": external_subscription_id}
