from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_ATTENTION_READ, PERMISSION_AUDIT_READ
from app.core.deps import admin_permissions_for, require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_operations import AdminAttentionResponse, AdminAuditEventsPage
from app.services.admin_attention_service import AdminAttentionService
from app.services.admin_audit_service import AdminAuditService

router = APIRouter(prefix="/admin", tags=["admin-operations"])


@router.get("/audit-events", response_model=AdminAuditEventsPage)
async def list_admin_audit_events(
    target_professional_id: UUID | None = Query(None, alias="targetProfessionalId"),
    actor_id: UUID | None = Query(None, alias="actorId"),
    action: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _: Professional = Depends(require_admin_permission(PERMISSION_AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminAuditService(db).list_events(
        target_professional_id=target_professional_id,
        actor_id=actor_id,
        action=action,
        page=page,
        limit=limit,
    )


@router.get("/attention", response_model=AdminAttentionResponse)
async def get_admin_attention(
    actor: Professional = Depends(require_admin_permission(PERMISSION_ATTENTION_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminAttentionService(db).build(set(admin_permissions_for(actor)))
