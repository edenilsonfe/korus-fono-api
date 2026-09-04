"""Admin endpoints for platform staff — professional accounts console."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import (
    PERMISSION_ACCOUNTS_READ,
    PERMISSION_ACCOUNTS_WRITE,
    PERMISSION_ADMIN_ROLES_WRITE,
    PERMISSION_BILLING_WRITE,
)
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_professional import (
    AdminHubStats,
    AdminProfessionalDetail,
    AdminProfessionalsPage,
    AdminReasonBody,
    ExtendTrialBody,
    GrantTemporaryAccessBody,
    TemporaryAccessReasonBody,
    SetStaffBody,
    SetSubscriptionStatusBody,
)
from app.schemas.admin_operations import SetAdminRoleBody
from app.services.admin_professional_service import (
    AdminBillingSyncError,
    AdminConflictError,
    AdminNotFoundError,
    AdminProfessionalService,
)

router = APIRouter(prefix="/admin", tags=["admin-professionals"])


def _service(db: AsyncSession) -> AdminProfessionalService:
    return AdminProfessionalService(db)


@router.get("/stats", response_model=AdminHubStats)
async def admin_hub_stats(
    _: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await _service(db).hub_stats()


@router.get("/professionals", response_model=AdminProfessionalsPage)
async def list_professionals(
    q: str | None = Query(None),
    subscription_status: str | None = Query(None, alias="subscriptionStatus"),
    is_staff: bool | None = Query(None, alias="isStaff"),
    is_disabled: bool | None = Query(None, alias="isDisabled"),
    specialty_key: str | None = Query(None, alias="specialtyKey"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await _service(db).list_professionals(
        q=q,
        subscription_status=subscription_status,
        is_staff=is_staff,
        is_disabled=is_disabled,
        specialty_key=specialty_key,
        page=page,
        limit=limit,
    )


@router.get("/professionals/{professional_id}", response_model=AdminProfessionalDetail)
async def get_professional(
    professional_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_READ)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).get_detail(professional_id)
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc


@router.post("/professionals/{professional_id}/extend-trial", response_model=AdminProfessionalDetail)
async def extend_trial(
    professional_id: UUID,
    body: ExtendTrialBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).extend_trial(
            actor=actor, professional_id=professional_id, days=body.days, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc


@router.post("/professionals/{professional_id}/temporary-access", response_model=AdminProfessionalDetail)
async def grant_temporary_access(
    professional_id: UUID,
    body: GrantTemporaryAccessBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).grant_temporary_access(
            actor=actor, professional_id=professional_id, days=body.days, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conta não encontrada") from exc
    except AdminConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.post(
    "/professionals/{professional_id}/temporary-access/revoke", response_model=AdminProfessionalDetail
)
async def revoke_temporary_access(
    professional_id: UUID,
    body: TemporaryAccessReasonBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).revoke_temporary_access(
            actor=actor, professional_id=professional_id, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conta não encontrada") from exc


@router.patch("/professionals/{professional_id}/staff", response_model=AdminProfessionalDetail)
async def set_staff(
    professional_id: UUID,
    body: SetStaffBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ADMIN_ROLES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).set_staff(
            actor=actor,
            professional_id=professional_id,
            is_staff=body.is_staff,
            reason=body.reason,
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc
    except AdminConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc


@router.patch(
    "/professionals/{professional_id}/subscription-status",
    response_model=AdminProfessionalDetail,
)
async def set_subscription_status(
    professional_id: UUID,
    body: SetSubscriptionStatusBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).set_subscription_status(
            actor=actor,
            professional_id=professional_id,
            status=body.status,
            reason=body.reason,
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc
    except AdminConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc


@router.post("/professionals/{professional_id}/disable", response_model=AdminProfessionalDetail)
async def disable_professional(
    professional_id: UUID,
    body: AdminReasonBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).disable(
            actor=actor, professional_id=professional_id, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc
    except AdminConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
    except AdminBillingSyncError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail) from exc


@router.post("/professionals/{professional_id}/enable", response_model=AdminProfessionalDetail)
async def enable_professional(
    professional_id: UUID,
    body: AdminReasonBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).enable(
            actor=actor, professional_id=professional_id, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc


@router.post(
    "/professionals/{professional_id}/invalidate-sessions",
    response_model=AdminProfessionalDetail,
)
async def invalidate_sessions(
    professional_id: UUID,
    body: AdminReasonBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ACCOUNTS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).invalidate_sessions(
            actor=actor, professional_id=professional_id, reason=body.reason
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc


@router.patch("/professionals/{professional_id}/admin-role", response_model=AdminProfessionalDetail)
async def set_admin_role(
    professional_id: UUID,
    body: SetAdminRoleBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_ADMIN_ROLES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await _service(db).set_admin_role(
            actor=actor,
            professional_id=professional_id,
            admin_role=body.admin_role,
            reason=body.reason,
        )
    except AdminNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc
    except AdminConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
