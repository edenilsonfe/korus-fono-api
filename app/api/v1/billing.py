"""Billing plan catalog, checkout and webhooks."""

import hmac
import json
import logging
import re
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.billing import PaymentGatewayConfigError, get_payment_gateway
from app.billing.checkout_urls import build_checkout_return_urls
from app.billing.errors import PaymentGatewayError
from app.billing.webhook_normalizer import get_normalizer
from app.core.client_ip import get_client_ip
from app.core.config import get_settings
from app.core.deps import get_current_professional
from app.db.session import get_db
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.schemas.billing import (
    BillingMeResponse,
    CardInvoiceResponse,
    CheckoutRequest,
    CheckoutResponse,
    PaymentSessionResponse,
    PixCheckoutResponse,
    PlanChangePreviewResponse,
    PlanPublicResponse,
    PlanSummary,
    PendingPlanSummary,
    ReconcileResponse,
    SubscriptionSummary,
)
from app.services.billing_checkout_service import BillingCheckoutService
from app.services.billing_customer_service import BillingCustomerService
from app.services.billing_reconciliation_service import BillingReconciliationService
from app.services.coupon_service import CouponError, CouponService
from app.services.entitlement_service import EntitlementService
from app.services.plan_change_service import PlanChangeService
from app.services.plan_catalog_seed import CANONICAL_PLAN_SLUGS
from app.services.saas_billing_service import SaasBillingService
from app.services.meta_pixel_service import MetaPixelService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

_CHECKOUT_GATEWAY_ERROR_DETAIL = (
    "Não foi possível iniciar o pagamento. Tente novamente em instantes."
)

# A cobrança é acessível logo após o cadastro. A sessão continua obrigatória,
# mas a verificação de e-mail permanece reservada às rotas clínicas.


async def track_checkout_started_task(
    professional_id: str,
    email: str,
    name: str,
    value_cents: int,
    currency: str,
    plan_slug: str,
    client_ip: str | None,
    client_user_agent: str | None,
    fbp: str | None,
    fbc: str | None,
) -> None:
    service = MetaPixelService()
    await service.track_checkout_started(
        professional_id=professional_id,
        email=email,
        name=name,
        value_cents=value_cents,
        currency=currency,
        plan_slug=plan_slug,
        client_ip=client_ip,
        client_user_agent=client_user_agent,
        fbp=fbp,
        fbc=fbc,
    )


def _digits_only(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _checkout_gateway_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=_CHECKOUT_GATEWAY_ERROR_DETAIL,
    )


def _plan_public(plan: Plan) -> PlanPublicResponse:
    return PlanPublicResponse(
        id=str(plan.id),
        slug=plan.slug,
        name=plan.name,
        description=plan.description,
        limits=plan.limits,
        price_cents=plan.price_cents,
        currency=plan.currency,
        billing_interval=plan.billing_interval,
        features=plan.features or [],
        badge=plan.badge,
        highlighted=plan.highlighted,
        display_order=plan.display_order,
        is_active=plan.is_active,
    )


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


async def _latest_subscription(db: AsyncSession, professional_id: UUID) -> Subscription | None:
    result = await db.execute(
        select(Subscription)
        .options(
            joinedload(Subscription.plan),
            joinedload(Subscription.pending_plan),
        )
        .where(Subscription.professional_id == professional_id)
        .order_by(Subscription.updated_at.desc())
    )
    return result.scalars().first()


async def _ensure_subscription(
    db: AsyncSession,
    *,
    professional_id: UUID,
    plan: Plan,
    provider: str,
) -> Subscription:
    sub = await _latest_subscription(db, professional_id)
    if sub and sub.status in ("incomplete", "trialing", "past_due"):
        sub.plan_id = plan.id
        sub.provider = provider
        sub.status = "incomplete"
        await db.commit()
        await db.refresh(sub)
        return sub

    sub = Subscription(
        professional_id=professional_id,
        plan_id=plan.id,
        status="incomplete",
        provider=provider,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def _attach_checkout_to_subscription(
    db: AsyncSession,
    *,
    professional_id: UUID,
    provider: str,
    session: dict,
) -> None:
    sub = await _latest_subscription(db, professional_id)
    if not sub:
        return

    sub.provider = provider
    external_sub_id = session.get("external_subscription_id")
    if external_sub_id:
        sub.external_subscription_id = str(external_sub_id)

    checkout_id = session.get("external_checkout_id") or session.get("session_id")
    if checkout_id:
        sub.external_checkout_id = str(checkout_id)

    await db.commit()


@router.get("/plans", response_model=list[PlanPublicResponse])
async def list_billing_plans(
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Plan)
        .where(Plan.is_active.is_(True), Plan.slug.in_(CANONICAL_PLAN_SLUGS))
        .order_by(Plan.display_order.asc(), Plan.price_cents.asc(), Plan.name.asc())
    )
    return [_plan_public(plan) for plan in result.scalars().all()]


@router.get("/me", response_model=BillingMeResponse)
async def get_billing_me(
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    try:
        gateway = get_payment_gateway()
    except PaymentGatewayConfigError:
        gateway = None

    change_svc = PlanChangeService(db, gateway)
    await change_svc.apply_scheduled_changes(professional.id)

    ent = EntitlementService(db)
    can_write = await ent.can_write(professional)
    sub = await _latest_subscription(db, professional.id)

    subscription_summary = None
    if sub and sub.plan:
        pending_plan_summary = None
        if sub.pending_plan:
            pending_plan_summary = PendingPlanSummary(
                id=str(sub.pending_plan.id),
                slug=sub.pending_plan.slug,
                name=sub.pending_plan.name,
                billing_interval=sub.pending_plan.billing_interval,
            )
        subscription_summary = SubscriptionSummary(
            id=str(sub.id),
            status=sub.status,
            plan=PlanSummary(
                id=str(sub.plan.id),
                slug=sub.plan.slug,
                name=sub.plan.name,
                billing_interval=sub.plan.billing_interval,
            ),
            started_at=_iso(sub.started_at),
            last_payment_at=_iso(sub.last_payment_at),
            current_period_end=_iso(sub.current_period_end),
            pending_plan=pending_plan_summary,
            pending_change_at=_iso(sub.pending_change_at),
        )

    return BillingMeResponse(
        subscription_status=professional.subscription_status,
        trial_started_at=_iso(professional.trial_started_at),
        trial_ends_at=_iso(professional.trial_ends_at),
        can_write=can_write,
        subscription=subscription_summary,
    )


@router.get("/plan-change/preview", response_model=PlanChangePreviewResponse)
async def preview_plan_change(
    plan_slug: str,
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    result = await db.execute(
        select(Plan).where(Plan.slug == plan_slug.strip(), Plan.is_active.is_(True))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plano não encontrado ou inativo")

    change_svc = PlanChangeService(db)
    preview = await change_svc.preview_change(professional=professional, target_plan=plan)
    return PlanChangePreviewResponse(**preview)


@router.post("/checkout", response_model=CheckoutResponse)
async def create_billing_checkout(
    payload: CheckoutRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    plan_slug = payload.plan_slug.strip()
    result = await db.execute(
        select(Plan).where(Plan.slug == plan_slug, Plan.is_active.is_(True))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plano não encontrado ou inativo")

    try:
        gateway = get_payment_gateway()
    except PaymentGatewayConfigError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    provider = getattr(gateway, "provider_key", "stub")
    success_url, cancel_url = build_checkout_return_urls()
    professional_id = str(professional.id)

    document = _digits_only(payload.cpf) or _digits_only(professional.cpf)
    if provider == "asaas" and not document:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe seu CPF para continuar com o pagamento pelo Asaas.",
        )

    if payload.cpf and payload.cpf != professional.cpf:
        professional.cpf = _digits_only(payload.cpf)
        await db.commit()

    existing_sub = await _latest_subscription(db, professional.id)
    if (
        existing_sub
        and existing_sub.status == "active"
        and professional.subscription_status == "active"
        and existing_sub.plan
        and existing_sub.plan.slug != plan_slug
    ):
        change_svc = PlanChangeService(db, gateway)
        change_result = await change_svc.initiate_change(
            professional=professional,
            subscription=existing_sub,
            target_plan=plan,
            document=document,
            provider=provider,
        )
        return CheckoutResponse(**change_result)

    await _ensure_subscription(db, professional_id=professional.id, plan=plan, provider=provider)
    existing_sub = await _latest_subscription(db, professional.id)

    charge_cents = plan.price_cents
    coupon_code_applied = None
    if payload.coupon_code:
        coupon_svc = CouponService(db)
        try:
            coupon = await coupon_svc.get_by_code(payload.coupon_code)
            await coupon_svc.validate_for_professional(coupon, professional.id, plan.slug)
            charge_cents = coupon_svc.discounted_price_cents(coupon, plan.price_cents)
            await coupon_svc.redeem(
                coupon=coupon, professional_id=professional.id, context="checkout"
            )
            if coupon.trial_bonus_days > 0:
                from datetime import UTC, datetime, timedelta

                base = professional.trial_ends_at or datetime.now(UTC)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=UTC)
                if base < datetime.now(UTC):
                    base = datetime.now(UTC)
                professional.trial_ends_at = base + timedelta(days=coupon.trial_bonus_days)
            coupon_code_applied = coupon.code
            await db.commit()
        except CouponError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc

    metadata: dict = {
        "professional_id": professional_id,
        "plan_id": str(plan.id),
        "plan_slug": plan.slug,
        "plan_name": plan.name,
        "price_cents": plan.price_cents,
        "charge_cents": charge_cents,
        "currency": plan.currency,
        "billing_interval": plan.billing_interval,
        "provider": provider,
        "customer_email": professional.email,
        "customer_name": professional.name,
    }
    if coupon_code_applied:
        metadata["coupon_code"] = coupon_code_applied

    if provider != "stub":
        if document:
            metadata["customer_document"] = document
        customer_svc = BillingCustomerService(db)
        try:
            metadata["customer_external_id"] = await customer_svc.ensure_customer(
                professional_id=professional_id,
                provider=provider,
                email=professional.email,
                name=professional.name,
                gateway=gateway,
                document=document or None,
            )
        except PaymentGatewayError as exc:
            logger.warning(
                "Billing checkout gateway failure provider=%s stage=%s professional_id=%s: %s",
                provider,
                "ensure_customer",
                professional_id,
                exc,
                exc_info=True,
            )
            raise _checkout_gateway_error() from exc
        if existing_sub and existing_sub.external_subscription_id:
            metadata["existing_external_subscription_id"] = existing_sub.external_subscription_id
            if existing_sub.external_checkout_id:
                metadata["existing_external_checkout_id"] = existing_sub.external_checkout_id

    try:
        session = await gateway.create_checkout_session(
            account_id=professional_id,
            plan_slug=plan.slug,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
        )
    except PaymentGatewayError as exc:
        logger.warning(
            "Billing checkout gateway failure provider=%s stage=%s professional_id=%s: %s",
            provider,
            "create_checkout_session",
            professional_id,
            exc,
            exc_info=True,
        )
        raise _checkout_gateway_error() from exc

    await _attach_checkout_to_subscription(
        db,
        professional_id=professional.id,
        provider=provider,
        session=session,
    )

    if session.get("status") == "completed":
        await BillingReconciliationService(db).reconcile_professional(professional.id)

    background_tasks.add_task(
        track_checkout_started_task,
        professional_id,
        professional.email,
        professional.name,
        charge_cents,
        plan.currency,
        plan.slug,
        get_client_ip(request),
        request.headers.get("user-agent"),
        request.cookies.get("_fbp"),
        request.cookies.get("_fbc"),
    )

    return CheckoutResponse(
        checkout_url=session["checkout_url"],
        session_id=session.get("session_id") or session.get("external_checkout_id"),
        status=session.get("status", "pending"),
        provider=provider,
        message="Continue para escolher PIX ou cartão.",
    )


@router.get("/checkout/{session_id}", response_model=PaymentSessionResponse)
async def get_checkout_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    service = BillingCheckoutService(db)
    return await service.get_session(session_id=session_id, professional=professional)


@router.post("/checkout/{session_id}/pix", response_model=PixCheckoutResponse)
async def generate_pix_checkout(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    service = BillingCheckoutService(db)
    return await service.generate_pix(session_id=session_id, professional=professional)


@router.post("/checkout/{session_id}/prepare-card", response_model=CardInvoiceResponse)
async def prepare_card_invoice(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    """Garante billingType=CREDIT_CARD e devolve invoiceUrl (form de cartão/parcelas no Asaas)."""
    service = BillingCheckoutService(db)
    return await service.prepare_card_invoice(
        session_id=session_id, professional=professional
    )


@router.post("/reconcile", response_model=ReconcileResponse)
async def reconcile_billing(
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    service = BillingReconciliationService(db)
    try:
        result = await service.reconcile_professional(professional.id)
    except PaymentGatewayError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ReconcileResponse(**result)


@router.post("/reconcile/simulate", response_model=ReconcileResponse)
async def simulate_stub_billing(
    db: AsyncSession = Depends(get_db),
    professional: Professional = Depends(get_current_professional),
):
    settings = get_settings()
    if not settings.debug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Simulação de pagamento disponível apenas em ambiente de desenvolvimento.",
        )
    service = BillingReconciliationService(db)
    result = await service.simulate_stub_payment(professional.id)
    return ReconcileResponse(**result)


@router.post("/webhooks/{provider}")
async def billing_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    provider_key = (provider or "").lower().strip()

    if provider_key == "asaas":
        webhook_token = (settings.asaas_webhook_token or "").strip()
        token = (request.headers.get("asaas-access-token") or "").strip()
        if not webhook_token or not token or not hmac.compare_digest(token, webhook_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Asaas webhook token")
    elif provider_key == "stub":
        if not settings.debug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    body_bytes = await request.body()
    try:
        body: dict = json.loads(body_bytes.decode() or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    normalizer = get_normalizer(provider_key)
    events = normalizer.normalize(body, dict(request.headers))
    billing = SaasBillingService(db)

    try:
        for ev in events:
            row = await billing.record_webhook_raw(
                provider=provider_key,
                external_event_id=ev.external_event_id,
                event_type=ev.event_type.value,
                payload=ev.payload,
                professional_id=ev.professional_hint,
            )
            if row:
                await billing.apply_normalized_events([ev])
                await billing.mark_processed(row.id)
    except Exception:
        logger.exception("Webhook processing failed for provider=%s", provider_key)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed",
        ) from None

    return {"received": True, "events": len(events)}
