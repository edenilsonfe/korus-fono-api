"""Admin endpoints — protocol catalog publication."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_PRODUCT_READ, PERMISSION_PRODUCT_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_product import AdminProtocolItem, AdminProtocolUpdate
from app.services.admin_protocol_service import (
    AdminProtocolConflictError,
    AdminProtocolNotFoundError,
    AdminProtocolService,
)

router = APIRouter(prefix="/admin/protocols", tags=["admin-protocols"])


@router.get("", response_model=list[AdminProtocolItem])
@router.get("/", response_model=list[AdminProtocolItem])
async def list_admin_protocols(
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminProtocolService(db).list_all()


@router.patch("/{protocol_id}", response_model=AdminProtocolItem)
async def update_admin_protocol(
    protocol_id: str,
    body: AdminProtocolUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await AdminProtocolService(db).update(
            actor=actor, protocol_id=protocol_id, body=body
        )
    except AdminProtocolNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Protocolo não encontrado") from exc
    except AdminProtocolConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
