"""Admin endpoints — feature flags."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_PRODUCT_READ, PERMISSION_PRODUCT_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_product import (
    FeatureFlagCreate,
    FeatureFlagItem,
    FeatureFlagUpdate,
    ProfessionalFlagState,
    SetFlagOverrideBody,
)
from app.services.feature_flag_service import (
    FeatureFlagConflictError,
    FeatureFlagNotFoundError,
    FeatureFlagService,
)

router = APIRouter(prefix="/admin", tags=["admin-feature-flags"])


@router.get("/feature-flags", response_model=list[FeatureFlagItem])
async def list_feature_flags(
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await FeatureFlagService(db).list_flags()


@router.post("/feature-flags", response_model=FeatureFlagItem, status_code=status.HTTP_201_CREATED)
async def create_feature_flag(
    body: FeatureFlagCreate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await FeatureFlagService(db).create_flag(actor=actor, body=body)
    except FeatureFlagConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc


@router.patch("/feature-flags/{key}", response_model=FeatureFlagItem)
async def update_feature_flag(
    key: str,
    body: FeatureFlagUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await FeatureFlagService(db).update_flag(actor=actor, key=key, body=body)
    except FeatureFlagNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag não encontrada") from exc


@router.get(
    "/professionals/{professional_id}/feature-flags",
    response_model=list[ProfessionalFlagState],
)
async def list_professional_feature_flags(
    professional_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await FeatureFlagService(db).list_professional_flags(professional_id)
    except FeatureFlagNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada") from exc


@router.put(
    "/professionals/{professional_id}/feature-flags/{key}",
    response_model=list[ProfessionalFlagState],
)
async def set_professional_feature_flag(
    professional_id: UUID,
    key: str,
    body: SetFlagOverrideBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await FeatureFlagService(db).set_override(
            actor=actor,
            professional_id=professional_id,
            key=key,
            enabled=body.enabled,
            reason=body.reason,
        )
    except FeatureFlagNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conta ou flag não encontrada"
        ) from exc
