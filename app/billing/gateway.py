"""Payment gateway protocol."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PaymentGateway(Protocol):
    provider_key: str

    async def create_customer(
        self, *, account_id: str, email: str, name: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def create_checkout_session(
        self,
        *,
        account_id: str,
        plan_slug: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def update_customer_document(self, *, customer_id: str, document: str) -> None: ...

    async def create_subscription(
        self,
        *,
        account_id: str,
        plan_slug: str,
        customer_external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_subscription(self, *, external_subscription_id: str) -> dict[str, Any]: ...

    async def get_subscription_status(self, *, external_subscription_id: str) -> dict[str, Any]: ...
