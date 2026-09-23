import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon, CouponRedemption
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.billing.asaas_gateway import AsaasPaymentGateway
from app.billing.errors import PaymentGatewayError
from app.schemas.admin_billing import (
    ApplyCouponResult,
    CouponCreate,
    CouponItem,
    CouponUpdate,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.affiliate_service import AffiliateService
from app.services.affiliate_credit_service import AffiliateCreditService


COUPON_TIMEZONE = ZoneInfo("America/Sao_Paulo")
RESERVATION_DURATION = timedelta(hours=24)
logger = logging.getLogger(__name__)


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, COUPON_TIMEZONE).astimezone(UTC)


def _after_end_of_day(value: date) -> datetime:
    return _start_of_day(value + timedelta(days=1))


def _civil_date(value: datetime | None, *, exclusive_end: bool = False) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if exclusive_end:
        value -= timedelta(microseconds=1)
    return value.astimezone(COUPON_TIMEZONE).date()


class CouponError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class CouponNotFoundError(CouponError):
    pass


class CouponService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AdminAuditService(db)

    @staticmethod
    def reservation_is_current(row: CouponRedemption) -> bool:
        expiry = row.reserved_until
        if expiry is None:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry > datetime.now(UTC)

    async def list_coupons(self) -> list[CouponItem]:
        result = await self.db.execute(select(Coupon).order_by(Coupon.created_at.desc()))
        coupons = result.scalars().all()
        items: list[CouponItem] = []
        for c in coupons:
            confirmed, reserved = await self._counts(c.id)
            item = self._to_item(c, confirmed, reserved)
            item.has_history = bool(await self.db.scalar(
                select(CouponRedemption.id).where(CouponRedemption.coupon_id == c.id).limit(1)
            ))
            items.append(item)
        return items

    async def _counts(self, coupon_id: UUID) -> tuple[int, int]:
        rows = await self.db.execute(
            select(CouponRedemption.state, func.count())
            .where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state.in_(("reserved", "confirmed")),
            )
            .group_by(CouponRedemption.state)
        )
        counts = {state: int(count) for state, count in rows.all()}
        return counts.get("confirmed", 0), counts.get("reserved", 0)

    @staticmethod
    def _validate_definition(
        coupon_type: str, value: int, valid_from: date | None, valid_until: date | None
    ) -> None:
        if value <= 0 or (coupon_type == "percent" and value >= 100):
            raise CouponError("O desconto deve ser positivo e menor que 100%")
        if valid_from and valid_until and valid_from > valid_until:
            raise CouponError("A data inicial deve ser anterior ou igual à data final")

    async def _validate_plans(self, plan_slugs: list[str] | None) -> None:
        if not plan_slugs:
            return
        if len(plan_slugs) != len(set(plan_slugs)):
            raise CouponError("Há planos repetidos no cupom")
        rows = await self.db.scalars(select(Plan.slug).where(Plan.slug.in_(plan_slugs)))
        if set(rows.all()) != set(plan_slugs):
            raise CouponError("Cupom contém plano não encontrado")

    async def create(self, *, actor: Professional, body: CouponCreate) -> CouponItem:
        code = body.code.strip().upper()
        self._validate_definition(body.coupon_type, body.value, body.valid_from, body.valid_until)
        await self._validate_plans(body.plan_slugs)
        existing = await self.db.execute(select(Coupon).where(Coupon.code == code))
        if existing.scalar_one_or_none():
            raise CouponError("Código de cupom já existe")
        coupon = Coupon(
            code=code,
            coupon_type=body.coupon_type,
            value=body.value,
            trial_bonus_days=body.trial_bonus_days,
            valid_from=_start_of_day(body.valid_from) if body.valid_from else None,
            valid_until=_after_end_of_day(body.valid_until) if body.valid_until else None,
            max_redemptions=body.max_redemptions,
            max_per_professional=1,
            plan_slugs=body.plan_slugs,
            is_active=body.is_active,
        )
        self.db.add(coupon)
        await self.audit.log(
            actor_id=actor.id,
            action="create_coupon",
            payload={"code": code, "reason": body.reason},
        )
        await self.db.commit()
        await self.db.refresh(coupon)
        return self._to_item(coupon, 0, 0)

    async def update(self, *, actor: Professional, coupon_id: UUID, body: CouponUpdate) -> CouponItem:
        coupon = await self.db.scalar(
            select(Coupon).where(Coupon.id == coupon_id).with_for_update()
        )
        if coupon is None:
            raise CouponNotFoundError("Cupom não encontrado")
        fields = body.model_fields_set
        if any(getattr(body, name) is None for name in ("coupon_type", "value", "trial_bonus_days", "is_active") if name in fields):
            raise CouponError("Tipo, valor, bônus e estado ativo não podem ser vazios")
        historical = await self.db.scalar(
            select(func.count()).select_from(CouponRedemption).where(
                CouponRedemption.coupon_id == coupon.id
            )
        )
        if historical and {"code", "coupon_type", "value"} & fields:
            raise CouponError("Código, tipo e valor não podem mudar após o primeiro uso")
        normalized_code = None
        if "code" in fields:
            if body.code is None:
                raise CouponError("Código não pode ser vazio")
            normalized_code = body.code.strip().upper()
            if len(normalized_code) < 2:
                raise CouponError("Código deve ter pelo menos 2 caracteres")
            duplicate = await self.db.scalar(select(Coupon.id).where(
                Coupon.code == normalized_code, Coupon.id != coupon.id,
            ))
            if duplicate:
                raise CouponError("Código de cupom já existe")
        if "trial_bonus_days" in fields and body.trial_bonus_days and not coupon.trial_bonus_days:
            raise CouponError("Bônus de trial só pode ser editado em cupons legados")
        next_type = body.coupon_type if body.coupon_type is not None else coupon.coupon_type
        next_value = body.value if body.value is not None else coupon.value
        next_start = body.valid_from if "valid_from" in fields else _civil_date(coupon.valid_from)
        next_end = (
            body.valid_until
            if "valid_until" in fields else _civil_date(coupon.valid_until, exclusive_end=True)
        )
        if {"coupon_type", "value"} & fields:
            self._validate_definition(next_type, next_value, next_start, next_end)
        elif next_start and next_end and next_start > next_end:
            raise CouponError("A data inicial deve ser anterior ou igual à data final")
        if "plan_slugs" in fields:
            await self._validate_plans(body.plan_slugs)
        confirmed, reserved = await self._counts(coupon.id)
        if "max_redemptions" in fields and body.max_redemptions is not None:
            if body.max_redemptions < confirmed + reserved:
                raise CouponError("O limite não pode ser menor que os usos e reservas atuais")
        if normalized_code is not None:
            coupon.code = normalized_code
        for name in ("coupon_type", "value", "trial_bonus_days", "max_redemptions", "plan_slugs", "is_active"):
            if name in fields:
                setattr(coupon, name, getattr(body, name))
        if "valid_from" in fields:
            coupon.valid_from = _start_of_day(body.valid_from) if body.valid_from else None
        if "valid_until" in fields:
            coupon.valid_until = _after_end_of_day(body.valid_until) if body.valid_until else None
        await self.audit.log(
            actor_id=actor.id,
            action="update_coupon",
            payload={"code": coupon.code, "reason": body.reason},
        )
        await self.db.commit()
        await self.db.refresh(coupon)
        return self._to_item(coupon, confirmed, reserved)

    async def get_by_code(self, code: str) -> Coupon:
        result = await self.db.execute(select(Coupon).where(Coupon.code == code.strip().upper()))
        coupon = result.scalar_one_or_none()
        if coupon is None:
            raise CouponNotFoundError("Cupom não encontrado")
        return coupon

    async def current_reservation(self, subscription_id: UUID) -> CouponRedemption | None:
        return await self.db.scalar(
            select(CouponRedemption).where(
                CouponRedemption.subscription_id == subscription_id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state == "reserved",
            ).order_by(CouponRedemption.created_at.desc()).limit(1)
        )

    async def validate_for_professional(
        self, coupon: Coupon, professional_id: UUID, plan_slug: str | None = None
    ) -> None:
        now = datetime.now(UTC)
        if not coupon.is_active:
            raise CouponError("Cupom inativo")
        valid_from = coupon.valid_from.replace(tzinfo=UTC) if coupon.valid_from and coupon.valid_from.tzinfo is None else coupon.valid_from
        valid_until = coupon.valid_until.replace(tzinfo=UTC) if coupon.valid_until and coupon.valid_until.tzinfo is None else coupon.valid_until
        if valid_from and now < valid_from:
            raise CouponError("Cupom ainda não é válido")
        if valid_until and now >= valid_until:
            raise CouponError("Cupom expirado")
        if coupon.plan_slugs and plan_slug and plan_slug not in coupon.plan_slugs:
            raise CouponError("Cupom não válido para este plano")
        if plan_slug is None:
            if coupon.trial_bonus_days <= 0:
                raise CouponError("Este cupom de desconto deve ser usado no checkout")
            prior_admin = await self.db.scalar(
                select(func.count()).select_from(CouponRedemption).where(
                    CouponRedemption.coupon_id == coupon.id,
                    CouponRedemption.professional_id == professional_id,
                    CouponRedemption.context == "admin",
                )
            )
            if prior_admin:
                raise CouponError("Bônus de trial já aplicado nesta conta")
            return
        if coupon.trial_bonus_days:
            raise CouponError("Este cupom concede bônus de trial apenas pelo administrador")
        paid = await self.db.scalar(
            select(Subscription.id).where(
                Subscription.professional_id == professional_id,
                Subscription.last_payment_at.is_not(None),
            ).limit(1)
        )
        if paid:
            raise CouponError("Cupom válido somente para a primeira assinatura paga")
        previous = await self.db.scalar(
            select(CouponRedemption.id).where(
                CouponRedemption.professional_id == professional_id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state.in_(("confirmed", "refunded")),
            ).limit(1)
        )
        if previous:
            raise CouponError("Esta conta já utilizou um cupom de assinatura")
        confirmed, reserved = await self._counts(coupon.id)
        own_reservation = await self.db.scalar(
            select(CouponRedemption.id).where(
                CouponRedemption.coupon_id == coupon.id,
                CouponRedemption.professional_id == professional_id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state == "reserved",
                CouponRedemption.reserved_until > now,
            ).limit(1)
        )
        if coupon.max_redemptions is not None and confirmed + reserved >= coupon.max_redemptions:
            if not own_reservation:
                raise CouponError("Cupom esgotado")

    def discounted_price_cents(self, coupon: Coupon, price_cents: int) -> int:
        if coupon.coupon_type == "percent":
            discount = price_cents * coupon.value // 100
            return max(0, price_cents - discount)
        if coupon.coupon_type == "fixed_cents":
            return max(0, price_cents - coupon.value)
        return price_cents

    async def preview(
        self, *, code: str, plan: Plan, professional: Professional
    ) -> dict:
        coupon = await self.get_by_code(code)
        await self.validate_for_professional(coupon, professional.id, plan.slug)
        coupon_price = self.discounted_price_cents(coupon, plan.price_cents)
        if coupon_price <= 0:
            raise CouponError("O desconto precisa deixar um valor a pagar")
        referral_bps, referral = await AffiliateService(self.db).referral_discount(professional.id)
        referral_price = plan.price_cents - plan.price_cents * referral_bps // 10000
        referral_wins = referral is not None and referral_bps > 0 and referral_price <= coupon_price
        first_charge = referral_price if referral_wins else coupon_price
        credit = min(first_charge, max(0, await AffiliateCreditService(self.db).credit_balance(professional.id)))
        return {
            "coupon_code": coupon.code,
            "plan_slug": plan.slug,
            "original_cents": plan.price_cents,
            "discount_cents": plan.price_cents - first_charge,
            "credit_cents": credit,
            "first_charge_cents": first_charge - credit,
            "renewal_cents": plan.price_cents,
            "applied_benefit": "referral" if referral_wins else "coupon",
        }

    async def reserve(
        self,
        *,
        coupon: Coupon,
        professional_id: UUID,
        subscription_id: UUID,
        checkout_session_id: UUID,
        plan_slug: str,
        discounted_price_cents: int,
        allow_replacement: bool = False,
    ) -> CouponRedemption:
        await self.db.scalar(
            select(Professional).where(Professional.id == professional_id).with_for_update()
        )
        coupon = await self.db.scalar(select(Coupon).where(Coupon.id == coupon.id).with_for_update())
        if coupon is None:
            raise CouponNotFoundError("Cupom não encontrado")
        reservations = list((await self.db.scalars(
            select(CouponRedemption).where(
                CouponRedemption.professional_id == professional_id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state == "reserved",
            ).with_for_update()
        )).all())
        current = next((
            r for r in reservations
            if r.coupon_id == coupon.id and r.subscription_id == subscription_id
            and r.plan_slug == plan_slug and r.checkout_session_id == checkout_session_id
        ), None)
        if current and not allow_replacement:
            if not self.reservation_is_current(current):
                raise CouponError("Checkout expirado; aguarde a conciliação da cobrança")
            return current
        await self.validate_for_professional(coupon, professional_id, plan_slug)
        if reservations and not allow_replacement:
            raise CouponError("Há outro checkout com cupom pendente nesta conta")
        confirmed, reserved = await self._counts(coupon.id)
        if coupon.max_redemptions is not None and confirmed + reserved >= coupon.max_redemptions:
            if not (allow_replacement and any(r.coupon_id == coupon.id for r in reservations)):
                raise CouponError("Cupom esgotado")
        row = CouponRedemption(
            coupon_id=coupon.id,
            professional_id=professional_id,
            context="checkout",
            state="reserved",
            subscription_id=subscription_id,
            checkout_session_id=checkout_session_id,
            plan_slug=plan_slug,
            discounted_price_cents=discounted_price_cents,
            reserved_until=datetime.now(UTC) + RESERVATION_DURATION,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def bind_payment(self, redemption_id: UUID, external_payment_id: str) -> None:
        row = await self.db.get(CouponRedemption, redemption_id)
        if row and row.state == "reserved":
            row.external_payment_id = external_payment_id
            await self.db.flush()

    async def release(self, redemption_id: UUID) -> None:
        row = await self.db.get(CouponRedemption, redemption_id)
        if row and row.state == "reserved":
            row.state = "released"
            row.released_at = datetime.now(UTC)
            await self.db.flush()

    async def apply_payment_event(
        self, *, subscription: Subscription, payment_ids: set[str], provider_event: str
    ) -> None:
        if not payment_ids:
            return
        rows = list((await self.db.scalars(
            select(CouponRedemption).where(
                CouponRedemption.subscription_id == subscription.id,
                CouponRedemption.context == "checkout",
                CouponRedemption.state.in_(("reserved", "confirmed")),
            ).with_for_update()
        )).all())
        for row in rows:
            if row.external_payment_id not in payment_ids:
                continue
            if provider_event == "PAYMENT_REFUNDED":
                row.state = "refunded"
                row.refunded_at = datetime.now(UTC)
            elif provider_event == "PAYMENT_DELETED" and row.state == "reserved":
                row.state = "released"
                row.released_at = datetime.now(UTC)
            elif provider_event in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "CHECKOUT_PAID", "CREDIT_SETTLED"} and row.state == "reserved":
                row.state = "confirmed"
                row.redeemed_at = datetime.now(UTC)
        await self.db.flush()

    async def expire_due(self, *, limit: int = 25) -> int:
        rows = list((await self.db.scalars(
            select(CouponRedemption).where(
                CouponRedemption.context == "checkout",
                CouponRedemption.state == "reserved",
                CouponRedemption.reserved_until <= datetime.now(UTC),
            ).order_by(CouponRedemption.reserved_until).limit(limit)
            .with_for_update(skip_locked=True)
        )).all())
        settled = 0
        for row in rows:
            sub = await self.db.get(Subscription, row.subscription_id) if row.subscription_id else None
            if sub is None:
                logger.warning("Coupon reservation %s has no subscription; keeping the slot", row.id)
                continue
            if sub.provider != "asaas":
                if sub.status == "active" and sub.last_payment_at:
                    row.state = "confirmed"
                    row.redeemed_at = datetime.now(UTC)
                else:
                    row.state = "released"
                    row.released_at = datetime.now(UTC)
                    sub.status = "canceled"
                    sub.external_checkout_id = None
                    sub.external_subscription_id = None
                    sub.checkout_session_id = None
                await self.db.commit()
                settled += 1
                continue
            payment_id = row.external_payment_id or sub.external_checkout_id
            if not payment_id:
                logger.warning("Coupon reservation %s lacks a provider charge; keeping the slot", row.id)
                continue
            gateway = AsaasPaymentGateway()
            try:
                try:
                    payment = await gateway.get_payment(payment_id)
                    resource = "payment"
                except PaymentGatewayError as exc:
                    if exc.status_code != 404:
                        raise
                    payment = await gateway.get_checkout(payment_id)
                    resource = "checkout"
                provider_status = str(payment.get("status") or "").upper()
                if provider_status in {"CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH", "PAID"}:
                    row.state = "confirmed"
                    row.redeemed_at = datetime.now(UTC)
                elif provider_status == "REFUNDED":
                    row.state = "refunded"
                    row.refunded_at = datetime.now(UTC)
                elif provider_status in {"PENDING", "OVERDUE", "AWAITING_RISK_ANALYSIS", "ACTIVE"}:
                    if sub.external_subscription_id:
                        await gateway.cancel_subscription(
                            external_subscription_id=sub.external_subscription_id
                        )
                    elif resource == "payment":
                        await gateway.delete_payment(payment_id)
                    else:
                        await gateway.cancel_checkout(payment_id)
                    row.state = "released"
                    row.released_at = datetime.now(UTC)
                    sub.status = "canceled"
                    sub.external_checkout_id = None
                    sub.external_subscription_id = None
                    sub.checkout_session_id = None
                else:
                    logger.warning(
                        "Coupon reservation %s has unresolved provider status %s",
                        row.id, provider_status,
                    )
                    continue
                await self.db.commit()
                settled += 1
                if provider_status in {"CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH", "PAID"}:
                    try:
                        from app.services.billing_reconciliation_service import BillingReconciliationService
                        await BillingReconciliationService(self.db).reconcile_professional(row.professional_id)
                    except Exception:
                        await self.db.rollback()
                        logger.exception("Paid coupon checkout %s awaits billing reconciliation", row.id)
            except PaymentGatewayError:
                await self.db.rollback()
                logger.exception("Coupon reservation %s awaits provider reconciliation", row.id)
        return settled

    async def redeem(
        self,
        *,
        coupon: Coupon,
        professional_id: UUID,
        context: str,
    ) -> CouponRedemption:
        redemption = CouponRedemption(
            coupon_id=coupon.id,
            professional_id=professional_id,
            context=context,
            redeemed_at=datetime.now(UTC),
        )
        self.db.add(redemption)
        await self.db.flush()
        return redemption

    async def apply_admin(
        self, *, actor: Professional, professional_id: UUID, code: str, reason: str | None
    ) -> ApplyCouponResult:
        pro = await self.db.get(Professional, professional_id)
        if pro is None:
            raise CouponNotFoundError("Conta não encontrada")
        coupon = await self.get_by_code(code)
        await self.validate_for_professional(coupon, professional_id)
        await self.redeem(coupon=coupon, professional_id=professional_id, context="admin")

        extended = 0
        if coupon.trial_bonus_days > 0:
            base = pro.trial_ends_at or datetime.now(UTC)
            if base.tzinfo is None:
                base = base.replace(tzinfo=UTC)
            if base < datetime.now(UTC):
                base = datetime.now(UTC)
            pro.trial_ends_at = base + timedelta(days=coupon.trial_bonus_days)
            if pro.subscription_status == "trial_expired":
                pro.subscription_status = "trialing"
            extended = coupon.trial_bonus_days

        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=professional_id,
            action="apply_coupon",
            payload={"code": coupon.code, "trial_bonus_days": extended, "reason": reason},
        )
        await self.db.commit()
        return ApplyCouponResult(
            coupon_code=coupon.code,
            trial_extended_days=extended,
            message=(
                f"Cupom aplicado. Trial estendido em {extended} dia(s)."
                if extended
                else "Cupom registrado (sem bônus de trial)."
            ),
        )

    @staticmethod
    def _to_item(c: Coupon, redemption_count: int, reserved_count: int) -> CouponItem:
        return CouponItem(
            id=str(c.id),
            code=c.code,
            coupon_type=c.coupon_type,
            value=c.value,
            trial_bonus_days=c.trial_bonus_days,
            valid_from=_civil_date(c.valid_from),
            valid_until=_civil_date(c.valid_until, exclusive_end=True),
            max_redemptions=c.max_redemptions,
            max_per_professional=c.max_per_professional,
            plan_slugs=list(c.plan_slugs) if c.plan_slugs else None,
            is_active=c.is_active,
            external_coupon_id=c.external_coupon_id,
            redemption_count=redemption_count,
            reserved_count=reserved_count,
        )
