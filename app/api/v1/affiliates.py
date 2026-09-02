from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.affiliate import (
    AffiliateCreditRedemptionBody,
    AffiliateCreditRedemptionResult,
    AffiliateDashboardResponse,
    AffiliateFiscalProfileBody,
    AffiliateFiscalProfileItem,
    AffiliateOptInBody,
    AffiliateOptInResponse,
    PublicAffiliateCodeResponse,
    AffiliatePayoutItem,
    AffiliatePayoutRequestBody,
)
from app.models.affiliate import (
    AffiliateFiscalProfile,
    AffiliateParticipant,
    AffiliatePayoutRequest,
)
from app.core.config import get_settings
from app.services.feature_flag_service import FeatureFlagService
from app.services.affiliate_credit_service import (
    AffiliateCreditForbiddenError,
    AffiliateCreditService,
)
from app.services.affiliate_payout_service import (
    AffiliatePayoutConflictError,
    AffiliatePayoutForbiddenError,
    AffiliatePayoutNotFoundError,
    AffiliatePayoutService,
)
from app.services.affiliate_service import (
    AffiliateConflictError,
    AffiliateForbiddenError,
    AffiliateNotFoundError,
    AffiliateService,
)

router = APIRouter(prefix="/affiliates", tags=["affiliates"])


async def _customer_participant(
    professional: Professional, db: AsyncSession
) -> AffiliateParticipant:
    participant = (
        await db.execute(
            select(AffiliateParticipant).where(
                AffiliateParticipant.professional_id == professional.id
            )
        )
    ).scalar_one_or_none()
    if participant is None or not participant.customer_enabled:
        raise HTTPException(status_code=403, detail="Ative o programa de indicação primeiro")
    return participant


@router.get("/public/{code}", response_model=PublicAffiliateCodeResponse)
async def public_affiliate_code(code: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await AffiliateService(db).resolve_public_code(code)
        flag = (
            "affiliate_customer_program"
            if result["mode"] == "customer"
            else "affiliate_partner_program"
        )
        if not await FeatureFlagService(db).is_globally_enabled(flag):
            raise AffiliateNotFoundError("Código de indicação indisponível")
        return result
    except AffiliateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc


@router.get("/me", response_model=AffiliateDashboardResponse)
async def affiliate_dashboard(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    result = await AffiliateService(db).customer_dashboard(professional)
    if not await FeatureFlagService(db).is_enabled(
        professional, "affiliate_customer_program"
    ):
        result["eligible"] = False
    return result


@router.post(
    "/customer/opt-in",
    response_model=AffiliateOptInResponse,
    status_code=status.HTTP_201_CREATED,
)
async def affiliate_customer_opt_in(
    body: AffiliateOptInBody,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    try:
        if not await FeatureFlagService(db).is_enabled(
            professional, "affiliate_customer_program"
        ):
            raise AffiliateForbiddenError("Programa de indicação ainda não está disponível")
        result = await AffiliateService(db).opt_in_customer(
            professional=professional,
            terms_version=body.terms_version,
        )
        await db.commit()
        return {
            "participantId": result.participant.id,
            "mode": result.mode,
            "code": result.code,
        }
    except AffiliateConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except AffiliateForbiddenError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


@router.post("/me/credit-redemptions", response_model=AffiliateCreditRedemptionResult)
async def redeem_affiliate_credit(
    body: AffiliateCreditRedemptionBody,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = AffiliateCreditService(db)
        converted = await service.convert_available_to_credit(
            professional=professional,
            amount_cents=body.amount_cents,
            idempotency_key=f"credit-redemption:{professional.id}:{body.request_id}",
        )
        await db.commit()
        return {
            "convertedCents": converted,
            "creditBalanceCents": await service.credit_balance(professional.id),
        }
    except AffiliateCreditForbiddenError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.get("/me/fiscal-profiles", response_model=list[AffiliateFiscalProfileItem])
async def list_my_affiliate_fiscal_profiles(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    participant = await _customer_participant(professional, db)
    return (
        await db.execute(
            select(AffiliateFiscalProfile)
            .where(AffiliateFiscalProfile.participant_id == participant.id)
            .order_by(AffiliateFiscalProfile.version.desc())
        )
    ).scalars().all()


@router.post(
    "/me/fiscal-profiles",
    response_model=AffiliateFiscalProfileItem,
    status_code=status.HTTP_201_CREATED,
)
async def submit_my_affiliate_fiscal_profile(
    body: AffiliateFiscalProfileBody,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    participant = await _customer_participant(professional, db)
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


@router.get("/me/payouts", response_model=list[AffiliatePayoutItem])
async def list_my_affiliate_payouts(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    participant = await _customer_participant(professional, db)
    return (
        await db.execute(
            select(AffiliatePayoutRequest)
            .where(AffiliatePayoutRequest.participant_id == participant.id)
            .order_by(AffiliatePayoutRequest.requested_at.desc())
        )
    ).scalars().all()


@router.post(
    "/me/payouts",
    response_model=AffiliatePayoutItem,
    status_code=status.HTTP_201_CREATED,
)
async def request_my_affiliate_payout(
    body: AffiliatePayoutRequestBody,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    participant = await _customer_participant(professional, db)
    enabled = get_settings().affiliate_cash_payouts_enabled and await FeatureFlagService(
        db
    ).is_enabled(professional, "affiliate_cash_payouts")
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


@router.delete("/me/payouts/{payout_id}", response_model=AffiliatePayoutItem)
async def cancel_my_affiliate_payout(
    payout_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    participant = await _customer_participant(professional, db)
    try:
        payout = await AffiliatePayoutService(db).cancel_payout(
            payout_id=payout_id,
            participant_id=participant.id,
        )
        await db.commit()
        await db.refresh(payout)
        return payout
    except AffiliatePayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
