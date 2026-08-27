"""Admin billing — subscriptions, plans, coupons, metrics."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_BILLING_READ, PERMISSION_BILLING_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_billing import (
    AdminPlanCreate,
    AdminPlanItem,
    AdminPlanUpdate,
    AdminSubscriptionDetail,
    AdminSubscriptionsPage,
    ApplyCouponBody,
    ApplyCouponResult,
    BillingMetrics,
    CouponCreate,
    CouponItem,
    CouponUpdate,
    ReconcileAdminResult,
)
from app.services.admin_billing_service import (
    AdminBillingConflictError,
    AdminBillingNotFoundError,
    AdminBillingService,
)
from app.services.coupon_service import CouponError, CouponNotFoundError, CouponService

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])


@router.get("/metrics", response_model=BillingMetrics)
async def billing_metrics(
    period_days: int = Query(30, alias="periodDays"),
    _: Professional = Depends(require_admin_permission(PERMISSION_BILLING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminBillingService(db).metrics(period_days)


@router.get("/subscriptions", response_model=AdminSubscriptionsPage)
async def list_subscriptions(
    status_filter: str | None = Query(None, alias="status"),
    plan_slug: str | None = Query(None, alias="planSlug"),
    query: str | None = Query(None, alias="q", max_length=120),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _: Professional = Depends(require_admin_permission(PERMISSION_BILLING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminBillingService(db).list_subscriptions(
        status=status_filter, plan_slug=plan_slug, query=query, page=page, limit=limit
    )


@router.get("/subscriptions/{subscription_id}", response_model=AdminSubscriptionDetail)
async def get_subscription(
    subscription_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_BILLING_READ)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await AdminBillingService(db).get_subscription(subscription_id)
    except AdminBillingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Assinatura não encontrada") from exc


@router.post(
    "/professionals/{professional_id}/reconcile",
    response_model=ReconcileAdminResult,
)
async def reconcile_professional(
    professional_id: UUID,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await AdminBillingService(db).reconcile(actor=actor, professional_id=professional_id)
    except AdminBillingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conta não encontrada") from exc


@router.get("/plans", response_model=list[AdminPlanItem])
async def list_admin_plans(
    _: Professional = Depends(require_admin_permission(PERMISSION_BILLING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AdminBillingService(db).list_plans()


@router.post("/plans", response_model=AdminPlanItem, status_code=status.HTTP_201_CREATED)
async def create_admin_plan(
    body: AdminPlanCreate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await AdminBillingService(db).create_plan(actor=actor, body=body)
    except AdminBillingConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.patch("/plans/{plan_id}", response_model=AdminPlanItem)
async def update_admin_plan(
    plan_id: UUID,
    body: AdminPlanUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await AdminBillingService(db).update_plan(actor=actor, plan_id=plan_id, body=body)
    except AdminBillingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Plano não encontrado") from exc


@router.get("/coupons", response_model=list[CouponItem])
async def list_coupons(
    _: Professional = Depends(require_admin_permission(PERMISSION_BILLING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await CouponService(db).list_coupons()


@router.post("/coupons", response_model=CouponItem, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    body: CouponCreate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await CouponService(db).create(actor=actor, body=body)
    except CouponError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.patch("/coupons/{coupon_id}", response_model=CouponItem)
async def update_coupon(
    coupon_id: UUID,
    body: CouponUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await CouponService(db).update(actor=actor, coupon_id=coupon_id, body=body)
    except CouponNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except CouponError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.post(
    "/professionals/{professional_id}/apply-coupon",
    response_model=ApplyCouponResult,
)
async def apply_coupon(
    professional_id: UUID,
    body: ApplyCouponBody,
    actor: Professional = Depends(require_admin_permission(PERMISSION_BILLING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await CouponService(db).apply_admin(
            actor=actor,
            professional_id=professional_id,
            code=body.code,
            reason=body.reason,
        )
    except CouponNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except CouponError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
