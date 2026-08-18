"""Persist and reuse PSP customer records per professional."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.gateway import PaymentGateway
from app.models.billing import BillingCustomer


class BillingCustomerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_external_customer_id(
        self, *, professional_id: str, provider: str
    ) -> str | None:
        result = await self.db.execute(
            select(BillingCustomer.external_customer_id).where(
                BillingCustomer.professional_id == UUID(professional_id),
                BillingCustomer.provider == provider,
            )
        )
        return result.scalar_one_or_none()

    async def ensure_customer(
        self,
        *,
        professional_id: str,
        provider: str,
        email: str,
        name: str,
        gateway: PaymentGateway,
        document: str | None = None,
    ) -> str:
        existing = await self.get_external_customer_id(
            professional_id=professional_id, provider=provider
        )
        if existing:
            if document:
                await gateway.update_customer_document(
                    customer_id=existing,
                    document=document,
                )
            return existing

        created = await gateway.create_customer(
            account_id=professional_id,
            email=email,
            name=name,
            metadata={"professional_id": professional_id, "customer_document": document},
        )
        external_id = str(created["external_customer_id"])

        self.db.add(
            BillingCustomer(
                professional_id=UUID(professional_id),
                provider=provider,
                external_customer_id=external_id,
            )
        )
        await self.db.commit()
        return external_id
