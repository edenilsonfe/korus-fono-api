from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.client_ip import get_client_ip
from app.db.session import get_db
from app.models.affiliate import (
    AffiliateCode,
    AffiliateFiscalProfile,
    AffiliateParticipant,
    AffiliatePayoutRequest,
    AffiliatePolicy,
    AffiliateReferral,
    AffiliateReward,
)
from app.schemas.affiliate import (
    AffiliateFiscalProfileBody,
    AffiliateFiscalProfileItem,
    AffiliatePayoutItem,
    AffiliatePayoutRequestBody,
    AffiliatePortalDashboard,
    AffiliatePortalExchangeBody,
    AffiliatePortalLinkRequest,
    AffiliatePortalSession,
    AffiliateOptInBody,
    AffiliateOptInResponse,
)
from app.schemas.common import MessageResponse
from app.services.affiliate_payout_service import (
    AffiliatePayoutConflictError,
    AffiliatePayoutForbiddenError,
    AffiliatePayoutService,
)
from app.services.affiliate_portal_service import (
    AffiliatePortalForbiddenError,
    AffiliatePortalService,
    send_affiliate_magic_link_email,
)
from app.services.affiliate_service import (
    AffiliateConflictError,
    AffiliateForbiddenError,
    AffiliateService,
)
from app.services.auth_rate_limit import enforce_forgot_rate_limit
from app.services.feature_flag_service import FeatureFlagService

router = APIRouter(prefix="/affiliate-portal", tags=["affiliate-portal"])
PORTAL_COOKIE = "korus_affiliate_portal"


async def get_portal_participant(
    portal_token: str | None = Cookie(None, alias=PORTAL_COOKIE),
    db: AsyncSession = Depends(get_db),
) -> AffiliateParticipant:
    if not portal_token:
        raise HTTPException(status_code=401, detail="Sessão do portal ausente")
    try:
        participant_id = AffiliatePortalService.decode_session_token(portal_token)
    except AffiliatePortalForbiddenError as exc:
        raise HTTPException(status_code=401, detail=exc.detail) from exc
    participant = await db.get(AffiliateParticipant, participant_id)
    if (
        participant is None
        or participant.status not in {"invited", "active"}
        or not participant.partner_enabled
    ):
        raise HTTPException(status_code=403, detail="Acesso ao portal suspenso")
    return participant


def _require_portal_mutation_header(request: Request) -> None:
    if request.headers.get("x-affiliate-portal") != "1":
        raise HTTPException(status_code=403, detail="Confirmação da sessão ausente")


async def _require_current_partner_terms(
    participant: AffiliateParticipant,
    db: AsyncSession,
) -> AffiliatePolicy:
    policy = (
        await db.execute(
            select(AffiliatePolicy).where(
                AffiliatePolicy.mode == "partner",
                AffiliatePolicy.status == "active",
            )
        )
    ).scalars().first()
    if policy is None:
        raise HTTPException(status_code=503, detail="Programa de parceiros indisponível")
    if participant.partner_terms_version != policy.terms_version:
        raise HTTPException(status_code=409, detail="Aceite a versão atual dos termos")
    return policy


@router.post("/request-link", response_model=MessageResponse)
async def request_portal_link(
    body: AffiliatePortalLinkRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    enforce_forgot_rate_limit(get_client_ip(request), str(body.email))
    if await FeatureFlagService(db).is_globally_enabled("affiliate_partner_program"):
        result = await AffiliatePortalService(db).request_magic_link(str(body.email))
        if result:
            participant, raw_token = result
            await db.commit()
            background_tasks.add_task(
                send_affiliate_magic_link_email,
                participant.email,
                participant.public_name,
                raw_token,
            )
    return MessageResponse(message="Se o cadastro estiver habilitado, enviaremos o link de acesso.")


@router.post("/exchange", response_model=AffiliatePortalSession)
async def exchange_portal_link(
    body: AffiliatePortalExchangeBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        participant = await AffiliatePortalService(db).exchange_magic_link(body.token)
    except AffiliatePortalForbiddenError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    token = AffiliatePortalService.create_session_token(participant)
    policy = (
        await db.execute(
            select(AffiliatePolicy).where(
                AffiliatePolicy.mode == "partner", AffiliatePolicy.status == "active"
            )
        )
    ).scalars().first()
    if policy is None:
        raise HTTPException(status_code=503, detail="Programa de parceiros indisponível")
    response.set_cookie(
        PORTAL_COOKIE,
        token,
        httponly=True,
        secure=not get_settings().debug,
        samesite="lax",
        max_age=8 * 60 * 60,
        path="/api/v1/affiliate-portal",
    )
    await db.commit()
    return {
        "participantId": participant.id,
        "publicName": participant.public_name,
        "status": participant.status,
        "partnerTermsVersion": participant.partner_terms_version,
        "requiredTermsVersion": policy.terms_version,
    }


@router.post("/logout", response_model=MessageResponse)
async def logout_portal(request: Request, response: Response):
    _require_portal_mutation_header(request)
    response.delete_cookie(
        PORTAL_COOKIE,
        path="/api/v1/affiliate-portal",
        secure=not get_settings().debug,
        httponly=True,
        samesite="lax",
    )
    return MessageResponse(message="Sessão encerrada")


@router.get("/me", response_model=AffiliatePortalSession)
async def portal_me(
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    policy = (
        await db.execute(
            select(AffiliatePolicy).where(
                AffiliatePolicy.mode == "partner", AffiliatePolicy.status == "active"
            )
        )
    ).scalars().first()
    if policy is None:
        raise HTTPException(status_code=503, detail="Programa de parceiros indisponível")
    return {
        "participantId": participant.id,
        "publicName": participant.public_name,
        "status": participant.status,
        "partnerTermsVersion": participant.partner_terms_version,
        "requiredTermsVersion": policy.terms_version,
    }


@router.get("/dashboard", response_model=AffiliatePortalDashboard)
async def portal_dashboard(
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    policy = await _require_current_partner_terms(participant, db)
    code = (
        await db.execute(
            select(AffiliateCode).where(
                AffiliateCode.participant_id == participant.id,
                AffiliateCode.mode == "partner",
                AffiliateCode.status == "active",
                AffiliateCode.terms_version == policy.terms_version,
            )
        )
    ).scalars().first()
    referrals = (
        await db.execute(
            select(AffiliateReferral)
            .where(AffiliateReferral.participant_id == participant.id)
            .order_by(AffiliateReferral.created_at.desc())
        )
    ).scalars().all()
    rewards = (
        await db.execute(
            select(AffiliateReward)
            .where(AffiliateReward.participant_id == participant.id)
            .order_by(AffiliateReward.created_at.desc())
        )
    ).scalars().all()
    return {
        "participant": participant,
        "code": code.code if code else None,
        "balances": await AffiliateService(db).balances(participant.id),
        "referrals": referrals,
        "rewards": rewards,
    }


@router.post("/accept-terms", response_model=AffiliateOptInResponse)
async def portal_accept_terms(
    body: AffiliateOptInBody,
    request: Request,
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    _require_portal_mutation_header(request)
    try:
        result = await AffiliateService(db).activate_partner(
            participant=participant,
            terms_version=body.terms_version,
        )
        await db.commit()
        return {
            "participantId": participant.id,
            "mode": result.mode,
            "code": result.code,
        }
    except AffiliateConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except AffiliateForbiddenError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


@router.post("/fiscal-profiles", response_model=AffiliateFiscalProfileItem)
async def portal_submit_fiscal_profile(
    body: AffiliateFiscalProfileBody,
    request: Request,
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    _require_portal_mutation_header(request)
    await _require_current_partner_terms(participant, db)
    try:
        profile = await AffiliatePayoutService(db).submit_fiscal_profile(
            participant=participant,
            person_type=body.person_type,
            legal_name=body.legal_name,
            document=body.document,
            pix_key_type=body.pix_key_type,
            pix_key=body.pix_key,
        )
        await db.commit()
        await db.refresh(profile)
        return profile
    except (AffiliatePayoutConflictError, AffiliatePayoutForbiddenError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.get("/fiscal-profiles", response_model=list[AffiliateFiscalProfileItem])
async def portal_list_fiscal_profiles(
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    return (
        await db.execute(
            select(AffiliateFiscalProfile)
            .where(AffiliateFiscalProfile.participant_id == participant.id)
            .order_by(AffiliateFiscalProfile.version.desc())
        )
    ).scalars().all()


@router.post("/payouts", response_model=AffiliatePayoutItem)
async def portal_request_payout(
    body: AffiliatePayoutRequestBody,
    request: Request,
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    _require_portal_mutation_header(request)
    await _require_current_partner_terms(participant, db)
    enabled = get_settings().affiliate_cash_payouts_enabled and await FeatureFlagService(
        db
    ).is_globally_enabled("affiliate_cash_payouts")
    try:
        payout = await AffiliatePayoutService(db).request_cash_payout(
            participant=participant,
            amount_cents=body.amount_cents,
            cash_enabled=enabled,
        )
        await db.commit()
        await db.refresh(payout)
        return payout
    except (AffiliatePayoutConflictError, AffiliatePayoutForbiddenError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.get("/payouts", response_model=list[AffiliatePayoutItem])
async def portal_list_payouts(
    participant: AffiliateParticipant = Depends(get_portal_participant),
    db: AsyncSession = Depends(get_db),
):
    return (
        await db.execute(
            select(AffiliatePayoutRequest)
            .where(AffiliatePayoutRequest.participant_id == participant.id)
            .order_by(AffiliatePayoutRequest.requested_at.desc())
        )
    ).scalars().all()
