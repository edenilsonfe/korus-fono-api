"""Service catalog rules shared by appointment create and update flows."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import ServiceOffering


async def get_active_service_for_appointment(
    db: AsyncSession,
    professional_id: UUID,
    service_id: UUID,
) -> ServiceOffering:
    result = await db.execute(
        select(ServiceOffering).where(
            ServiceOffering.id == service_id,
            ServiceOffering.professional_id == professional_id,
            ServiceOffering.active.is_(True),
        )
    )
    service = result.scalar_one_or_none()
    if not service:
        raise HTTPException(status_code=404, detail="Serviço financeiro não encontrado")
    return service


def service_snapshot(service: ServiceOffering) -> dict[str, object]:
    return {
        "service_id": service.id,
        "service_name_snapshot": service.name,
        "service_price_cents": service.price_cents,
    }
