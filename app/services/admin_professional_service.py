from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayError
from app.core.admin_permissions import ADMIN_ROLE_SUPERADMIN, resolve_admin_role
from app.models.ai import AIJob
from app.models.assessment import Assessment
from app.models.billing import Subscription
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.models.whatsapp_connection import WhatsAppConnection
from app.schemas.admin_professional import (
    AdminAIJobSummary,
    AdminHubStats,
    AdminPlanSummary,
    AdminProfessionalCounts,
    AdminProfessionalDetail,
    AdminProfessionalListItem,
    AdminProfessionalsPage,
    AdminWhatsAppSummary,
)
from app.services.admin_audit_service import AdminAuditService

ALLOWED_SUBSCRIPTION_STATUSES = frozenset(
    {"trialing", "active", "trial_expired", "past_due", "canceled"}
)


class AdminConflictError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class AdminNotFoundError(Exception):
    pass


class AdminBillingSyncError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def mask_cpf(cpf: str | None) -> str:
    digits = "".join(ch for ch in (cpf or "") if ch.isdigit())
    if len(digits) < 2:
        return "***.***.***-**"
    return f"***.***.***-{digits[-2:]}"


class AdminProfessionalService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AdminAuditService(db)

    async def hub_stats(self) -> AdminHubStats:
        async def _count(where):
            result = await self.db.execute(select(func.count()).select_from(Professional).where(where))
            return int(result.scalar_one())

        return AdminHubStats(
            trialing=await _count(Professional.subscription_status == "trialing"),
            trial_expired=await _count(Professional.subscription_status == "trial_expired"),
            active=await _count(Professional.subscription_status == "active"),
            staff=await _count(Professional.is_staff.is_(True)),
            disabled=await _count(Professional.is_disabled.is_(True)),
        )

    async def list_professionals(
        self,
        *,
        q: str | None = None,
        subscription_status: str | None = None,
        is_staff: bool | None = None,
        is_disabled: bool | None = None,
        specialty_key: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> AdminProfessionalsPage:
        page = max(page, 1)
        limit = min(max(limit, 1), 100)
        filters = []
        if q:
            term = f"%{q.strip()}%"
            filters.append(
                or_(
                    Professional.email.ilike(term),
                    Professional.name.ilike(term),
                    Professional.cpf.ilike(term),
                )
            )
        if subscription_status:
            filters.append(Professional.subscription_status == subscription_status)
        if is_staff is not None:
            filters.append(Professional.is_staff.is_(is_staff))
        if is_disabled is not None:
            filters.append(Professional.is_disabled.is_(is_disabled))
        if specialty_key:
            filters.append(Professional.specialty_key == specialty_key)

        count_stmt = select(func.count()).select_from(Professional)
        list_stmt = select(Professional).order_by(Professional.created_at.desc())
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        total = int((await self.db.execute(count_stmt)).scalar_one())
        result = await self.db.execute(list_stmt.offset((page - 1) * limit).limit(limit))
        rows = result.scalars().all()
        items = [
            AdminProfessionalListItem(
                id=str(p.id),
                name=p.name,
                email=p.email,
                specialty_key=p.specialty_key,
                subscription_status=p.subscription_status,
                trial_ends_at=p.trial_ends_at,
                is_staff=p.is_staff,
                admin_role=resolve_admin_role(admin_role=p.admin_role, is_staff=p.is_staff),
                is_disabled=p.is_disabled,
                created_at=p.created_at,
            )
            for p in rows
        ]
        return AdminProfessionalsPage(items=items, total=total, page=page, limit=limit)

    async def get_detail(self, professional_id: UUID) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)

        patients = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Patient).where(Patient.professional_id == pro.id)
                )
            ).scalar_one()
        )
        sessions = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Session).where(Session.professional_id == pro.id)
                )
            ).scalar_one()
        )
        assessments = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Assessment)
                    .where(Assessment.professional_id == pro.id)
                )
            ).scalar_one()
        )

        wa_result = await self.db.execute(
            select(WhatsAppConnection)
            .where(WhatsAppConnection.professional_id == pro.id)
            .order_by(WhatsAppConnection.updated_at.desc())
            .limit(1)
        )
        wa = wa_result.scalar_one_or_none()

        jobs_result = await self.db.execute(
            select(AIJob)
            .where(AIJob.professional_id == pro.id)
            .order_by(AIJob.created_at.desc())
            .limit(10)
        )
        jobs = jobs_result.scalars().all()

        sub_result = await self.db.execute(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.professional_id == pro.id)
            .order_by(Subscription.updated_at.desc())
            .limit(1)
        )
        sub = sub_result.scalar_one_or_none()
        plan = None
        if sub and sub.plan:
            plan = AdminPlanSummary(slug=sub.plan.slug, name=sub.plan.name, status=sub.status)

        return AdminProfessionalDetail(
            id=str(pro.id),
            name=pro.name,
            email=pro.email,
            phone=pro.phone,
            cpf_masked=mask_cpf(pro.cpf),
            specialty=pro.specialty,
            specialty_key=pro.specialty_key,
            council=pro.council,
            is_staff=pro.is_staff,
            admin_role=resolve_admin_role(admin_role=pro.admin_role, is_staff=pro.is_staff),
            is_disabled=pro.is_disabled,
            subscription_status=pro.subscription_status,
            trial_started_at=pro.trial_started_at,
            trial_ends_at=pro.trial_ends_at,
            created_at=pro.created_at,
            updated_at=pro.updated_at,
            plan=plan,
            counts=AdminProfessionalCounts(
                patients=patients, sessions=sessions, assessments=assessments
            ),
            whatsapp=AdminWhatsAppSummary(
                status=wa.status if wa else None,
                updated_at=wa.updated_at if wa else None,
            ),
            recent_ai_jobs=[
                AdminAIJobSummary(
                    id=str(j.id),
                    job_type=j.job_type,
                    status=j.status,
                    created_at=j.created_at,
                )
                for j in jobs
            ],
        )

    async def extend_trial(
        self, *, actor: Professional, professional_id: UUID, days: int, reason: str | None
    ) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)
        before = pro.trial_ends_at.isoformat() if pro.trial_ends_at else None
        base = pro.trial_ends_at or datetime.now(UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        if base < datetime.now(UTC):
            base = datetime.now(UTC)
        pro.trial_ends_at = base + timedelta(days=days)
        if pro.subscription_status == "trial_expired":
            pro.subscription_status = "trialing"
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=pro.id,
            action="extend_trial",
            payload={
                "days": days,
                "reason": reason,
                "trial_ends_at_before": before,
                "trial_ends_at_after": pro.trial_ends_at.isoformat(),
                "subscription_status": pro.subscription_status,
            },
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def set_staff(
        self, *, actor: Professional, professional_id: UUID, is_staff: bool, reason: str | None
    ) -> AdminProfessionalDetail:
        return await self.set_admin_role(
            actor=actor,
            professional_id=professional_id,
            admin_role=ADMIN_ROLE_SUPERADMIN if is_staff else None,
            reason=reason or "Alteração de acesso administrativo legado",
            action="set_staff",
        )

    async def set_admin_role(
        self,
        *,
        actor: Professional,
        professional_id: UUID,
        admin_role: str | None,
        reason: str,
        action: str = "set_admin_role",
    ) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)
        before = resolve_admin_role(admin_role=pro.admin_role, is_staff=pro.is_staff)
        if pro.id == actor.id and admin_role != ADMIN_ROLE_SUPERADMIN:
            raise AdminConflictError("Você não pode reduzir o próprio papel de superadmin")
        if before == ADMIN_ROLE_SUPERADMIN and admin_role != ADMIN_ROLE_SUPERADMIN:
            remaining = int(
                (
                    await self.db.execute(
                        select(func.count())
                        .select_from(Professional)
                        .where(
                            Professional.id != pro.id,
                            Professional.is_disabled.is_(False),
                            or_(
                                Professional.admin_role == ADMIN_ROLE_SUPERADMIN,
                                and_(
                                    Professional.admin_role.is_(None),
                                    Professional.is_staff.is_(True),
                                ),
                            ),
                        )
                    )
                ).scalar_one()
            )
            if remaining == 0:
                raise AdminConflictError("A plataforma deve manter ao menos um superadmin ativo")

        pro.admin_role = admin_role
        pro.is_staff = admin_role is not None
        pro.token_version += 1
        await self.audit.log(
            actor=actor,
            target_professional_id=pro.id,
            action=action,
            payload={"before": before, "after": admin_role, "reason": reason},
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def set_subscription_status(
        self, *, actor: Professional, professional_id: UUID, status: str, reason: str | None
    ) -> AdminProfessionalDetail:
        if status not in ALLOWED_SUBSCRIPTION_STATUSES:
            raise AdminConflictError("Status de assinatura inválido")
        pro = await self._get(professional_id)
        before = pro.subscription_status
        pro.subscription_status = status
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=pro.id,
            action="set_subscription_status",
            payload={"before": before, "after": status, "reason": reason},
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def _cancel_asaas_subscriptions(self, professional: Professional) -> int:
        result = await self.db.execute(
            select(Subscription).where(
                Subscription.professional_id == professional.id,
                Subscription.provider == "asaas",
                Subscription.external_subscription_id.is_not(None),
                Subscription.status.in_(("active", "trialing", "incomplete", "past_due")),
            )
        )
        subscriptions = list(result.scalars().all())
        if not subscriptions:
            return 0

        gateway = AsaasPaymentGateway()
        for subscription in subscriptions:
            try:
                await gateway.cancel_subscription(
                    external_subscription_id=str(subscription.external_subscription_id)
                )
            except PaymentGatewayError as exc:
                if exc.status_code != 404:
                    raise AdminBillingSyncError(
                        "Não foi possível encerrar a recorrência no Asaas; a conta não foi desativada"
                    ) from exc
            subscription.status = "canceled"
        professional.subscription_status = "canceled"
        return len(subscriptions)

    async def disable(
        self, *, actor: Professional, professional_id: UUID, reason: str | None
    ) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)
        if pro.id == actor.id:
            raise AdminConflictError("Você não pode desativar a própria conta")
        canceled_subscriptions = await self._cancel_asaas_subscriptions(pro)
        pro.is_disabled = True
        pro.token_version += 1
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=pro.id,
            action="disable",
            payload={
                "reason": reason,
                "token_version": pro.token_version,
                "canceled_asaas_subscriptions": canceled_subscriptions,
            },
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def enable(
        self, *, actor: Professional, professional_id: UUID, reason: str | None
    ) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)
        pro.is_disabled = False
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=pro.id,
            action="enable",
            payload={"reason": reason},
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def invalidate_sessions(
        self, *, actor: Professional, professional_id: UUID, reason: str | None
    ) -> AdminProfessionalDetail:
        pro = await self._get(professional_id)
        before = pro.token_version
        pro.token_version += 1
        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=pro.id,
            action="invalidate_sessions",
            payload={"reason": reason, "before": before, "after": pro.token_version},
        )
        await self.db.commit()
        return await self.get_detail(pro.id)

    async def _get(self, professional_id: UUID) -> Professional:
        pro = await self.db.get(Professional, professional_id)
        if pro is None:
            raise AdminNotFoundError()
        return pro
