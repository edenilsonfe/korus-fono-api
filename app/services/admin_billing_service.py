from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models.billing import BillingEvent, Plan, Subscription
from app.models.professional import Professional
from app.schemas.admin_billing import (
    AdminBillingEventItem,
    AdminPlanCreate,
    AdminPlanItem,
    AdminPlanUpdate,
    AdminSubscriptionDetail,
    AdminSubscriptionListItem,
    AdminSubscriptionsPage,
    BillingMetrics,
    ReconcileAdminResult,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.billing_reconciliation_service import BillingReconciliationService


class AdminBillingNotFoundError(Exception):
    pass


class AdminBillingConflictError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class AdminBillingService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AdminAuditService(db)

    async def list_subscriptions(
        self,
        *,
        status: str | None = None,
        plan_slug: str | None = None,
        query: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> AdminSubscriptionsPage:
        page = max(page, 1)
        limit = min(max(limit, 1), 100)
        filters = []
        if status:
            filters.append(Subscription.status == status)
        if query:
            normalized_query = f"%{query.strip()}%"
            filters.append(
                or_(
                    Professional.name.ilike(normalized_query),
                    Professional.email.ilike(normalized_query),
                    Subscription.external_subscription_id.ilike(normalized_query),
                )
            )

        stmt = (
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.professional))
            .join(Professional, Subscription.professional_id == Professional.id)
            .order_by(Subscription.updated_at.desc())
        )
        count_stmt = (
            select(func.count())
            .select_from(Subscription)
            .join(Professional, Subscription.professional_id == Professional.id)
        )
        if plan_slug:
            stmt = stmt.join(Plan, Subscription.plan_id == Plan.id).where(Plan.slug == plan_slug)
            count_stmt = count_stmt.join(Plan, Subscription.plan_id == Plan.id).where(
                Plan.slug == plan_slug
            )
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        total = int((await self.db.execute(count_stmt)).scalar_one())
        rows = (
            await self.db.execute(stmt.offset((page - 1) * limit).limit(limit))
        ).scalars().unique().all()
        items = [self._list_item(s) for s in rows]
        return AdminSubscriptionsPage(items=items, total=total, page=page, limit=limit)

    async def get_subscription(self, subscription_id: UUID) -> AdminSubscriptionDetail:
        result = await self.db.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan), joinedload(Subscription.professional))
            .where(Subscription.id == subscription_id)
        )
        sub = result.scalars().unique().first()
        if sub is None:
            raise AdminBillingNotFoundError()
        events = (
            await self.db.execute(
                select(BillingEvent)
                .where(BillingEvent.professional_id == sub.professional_id)
                .order_by(BillingEvent.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
        base = self._list_item(sub)
        return AdminSubscriptionDetail(
            **base.model_dump(),
            professional_subscription_status=sub.professional.subscription_status,
            started_at=sub.started_at,
            last_payment_at=sub.last_payment_at,
            current_period_end=sub.current_period_end,
            recent_events=[
                AdminBillingEventItem(
                    id=str(e.id),
                    provider=e.provider,
                    external_event_id=e.external_event_id,
                    event_type=e.event_type,
                    status=e.status,
                    created_at=e.created_at,
                    processed_at=e.processed_at,
                )
                for e in events
            ],
        )

    async def reconcile(
        self, *, actor: Professional, professional_id: UUID
    ) -> ReconcileAdminResult:
        pro = await self.db.get(Professional, professional_id)
        if pro is None:
            raise AdminBillingNotFoundError()
        result = await BillingReconciliationService(self.db).reconcile_professional(professional_id)
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=professional_id,
            action="reconcile_billing",
            payload=result,
        )
        await self.db.commit()
        return ReconcileAdminResult(**result)

    async def list_plans(self) -> list[AdminPlanItem]:
        result = await self.db.execute(
            select(Plan).order_by(Plan.display_order.asc(), Plan.name.asc())
        )
        return [self._plan_item(p) for p in result.scalars().all()]

    async def create_plan(self, *, actor: Professional, body: AdminPlanCreate) -> AdminPlanItem:
        existing = await self.db.execute(select(Plan).where(Plan.slug == body.slug))
        if existing.scalar_one_or_none():
            raise AdminBillingConflictError("Slug de plano já existe")
        plan = Plan(
            slug=body.slug,
            name=body.name,
            description=body.description,
            price_cents=body.price_cents,
            currency=body.currency,
            billing_interval=body.billing_interval,
            features=body.features,
            badge=body.badge,
            highlighted=body.highlighted,
            display_order=body.display_order,
            is_active=body.is_active,
        )
        self.db.add(plan)
        await self.audit.log(
            actor_id=actor.id,
            action="create_plan",
            payload={"slug": body.slug, "reason": body.reason},
        )
        await self.db.commit()
        await self.db.refresh(plan)
        return self._plan_item(plan)

    async def update_plan(
        self, *, actor: Professional, plan_id: UUID, body: AdminPlanUpdate
    ) -> AdminPlanItem:
        plan = await self.db.get(Plan, plan_id)
        if plan is None:
            raise AdminBillingNotFoundError()
        before_price = plan.price_cents
        for field in (
            "name",
            "description",
            "price_cents",
            "billing_interval",
            "features",
            "badge",
            "highlighted",
            "display_order",
            "is_active",
        ):
            value = getattr(body, field)
            if value is not None:
                setattr(plan, field, value)
        await self.audit.log(
            actor_id=actor.id,
            action="update_plan",
            payload={
                "slug": plan.slug,
                "price_before": before_price,
                "price_after": plan.price_cents,
                "reason": body.reason,
            },
        )
        await self.db.commit()
        await self.db.refresh(plan)
        return self._plan_item(plan)

    async def metrics(self, period_days: int = 30) -> BillingMetrics:
        period_days = period_days if period_days in (7, 30, 90) else 30
        since = datetime.now(UTC) - timedelta(days=period_days)

        active_subs = (
            await self.db.execute(
                select(Subscription)
                .options(selectinload(Subscription.plan))
                .where(Subscription.status == "active")
            )
        ).scalars().unique().all()
        mrr = 0
        for sub in active_subs:
            if not sub.plan:
                continue
            if sub.plan.billing_interval == "yearly":
                mrr += sub.plan.price_cents // 12
            else:
                mrr += sub.plan.price_cents

        churn = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Professional)
                    .where(
                        Professional.subscription_status.in_(("canceled", "past_due")),
                        Professional.updated_at >= since,
                    )
                )
            ).scalar_one()
        )
        new_trials = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Professional)
                    .where(
                        Professional.trial_started_at.is_not(None),
                        Professional.trial_started_at >= since,
                    )
                )
            ).scalar_one()
        )
        conversions = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Subscription)
                    .where(
                        Subscription.status == "active",
                        Subscription.started_at.is_not(None),
                        Subscription.started_at >= since,
                    )
                )
            ).scalar_one()
        )
        trial_expired = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Professional)
                    .where(
                        Professional.subscription_status == "trial_expired",
                        Professional.updated_at >= since,
                    )
                )
            ).scalar_one()
        )
        return BillingMetrics(
            period_days=period_days,
            mrr_cents=mrr,
            churn_count=churn,
            new_trials=new_trials,
            conversions=conversions,
            trial_expired=trial_expired,
        )

    def _list_item(self, sub: Subscription) -> AdminSubscriptionListItem:
        return AdminSubscriptionListItem(
            id=str(sub.id),
            professional_id=str(sub.professional_id),
            professional_name=sub.professional.name if sub.professional else "",
            professional_email=sub.professional.email if sub.professional else "",
            plan_slug=sub.plan.slug if sub.plan else None,
            plan_name=sub.plan.name if sub.plan else None,
            status=sub.status,
            provider=sub.provider,
            external_subscription_id=sub.external_subscription_id,
            external_checkout_id=sub.external_checkout_id,
            updated_at=sub.updated_at,
        )

    @staticmethod
    def _plan_item(plan: Plan) -> AdminPlanItem:
        return AdminPlanItem(
            id=str(plan.id),
            slug=plan.slug,
            name=plan.name,
            description=plan.description,
            price_cents=plan.price_cents,
            currency=plan.currency,
            billing_interval=plan.billing_interval,
            features=plan.features or [],
            badge=plan.badge,
            highlighted=plan.highlighted,
            display_order=plan.display_order,
            is_active=plan.is_active,
        )
