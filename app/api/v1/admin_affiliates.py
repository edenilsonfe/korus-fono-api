from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.errors import PaymentGatewayConfigError, PaymentGatewayError
from app.core.admin_permissions import (
    PERMISSION_AFFILIATES_PAYOUT,
    PERMISSION_AFFILIATES_READ,
    PERMISSION_AFFILIATES_WRITE,
)
from app.core.config import get_settings
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.affiliate import (
    AffiliateFiscalProfile,
    AffiliateLedgerEntry,
    AffiliatePayoutBatch,
    AffiliatePayoutRequest,
    AffiliatePolicy,
    AffiliateReferral,
    AffiliateReward,
)
from app.models.professional import Professional
from app.schemas.affiliate import (
    AdminAffiliateCorrectionBody,
    AdminAffiliateCorrectionResult,
    AdminAffiliateFiscalApprovalBody,
    AdminAffiliateOverview,
    AdminAffiliateParticipantItem,
    AdminAffiliateParticipantStatusBody,
    AdminAffiliatePartnerInvite,
    AdminAffiliatePayoutCancelBody,
    AdminAffiliatePolicyCreate,
    AdminAffiliatePolicyItem,
    AdminAffiliateReviewBody,
    AdminAffiliateTransferBody,
    AffiliateFiscalProfileItem,
    AffiliatePayoutBatchItem,
    AffiliatePayoutItem,
    AffiliateReferralSummary,
    AffiliateRewardSummary,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.affiliate_accounting import lock_participant
from app.services.affiliate_payout_service import (
    AffiliatePayoutConflictError,
    AffiliatePayoutNotFoundError,
    AffiliatePayoutService,
)
from app.services.affiliate_service import (
    AffiliateConflictError,
    AffiliateForbiddenError,
    AffiliateNotFoundError,
    AffiliateService,
)

router = APIRouter(prefix="/admin/affiliates", tags=["admin-affiliates"])


@router.get("/overview", response_model=AdminAffiliateOverview)
async def affiliate_overview(
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AffiliateService(db).admin_overview()


@router.get("/policies", response_model=list[AdminAffiliatePolicyItem])
async def list_affiliate_policies(
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    return (
        (
            await db.execute(
                select(AffiliatePolicy).order_by(
                    AffiliatePolicy.mode, AffiliatePolicy.version.desc()
                )
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/policies",
    response_model=AdminAffiliatePolicyItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_affiliate_policy(
    body: AdminAffiliatePolicyCreate,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_WRITE)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        policy = await AffiliateService(db).create_policy(
            actor=actor,
            values=body.model_dump(exclude={"activate"}),
            activate=body.activate,
        )
    except (AffiliateConflictError, AffiliateForbiddenError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.policy.created",
        payload={
            "policyId": str(policy.id),
            "mode": policy.mode,
            "version": policy.version,
        },
    )
    await db.commit()
    await db.refresh(policy)
    return policy


@router.get("/participants", response_model=list[AdminAffiliateParticipantItem])
async def list_affiliate_participants(
    query: str | None = Query(None, alias="q", max_length=120),
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await AffiliateService(db).list_participants(query=query)


@router.post(
    "/participants/invite-partner",
    response_model=AdminAffiliateParticipantItem,
    status_code=status.HTTP_201_CREATED,
)
async def invite_affiliate_partner(
    body: AdminAffiliatePartnerInvite,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_WRITE)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        participant = await AffiliateService(db).invite_partner(
            email=str(body.email),
            public_name=body.public_name,
            commission_override_bps=body.commission_override_bps,
        )
    except (AffiliateConflictError, AffiliateForbiddenError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.partner.invited",
        payload={"participantId": str(participant.id)},
    )
    await db.commit()
    rows = await AffiliateService(db).list_participants(query=participant.email)
    return rows[0]


@router.patch(
    "/participants/{participant_id}/status",
    response_model=AdminAffiliateParticipantItem,
)
async def update_affiliate_participant_status(
    participant_id: UUID,
    body: AdminAffiliateParticipantStatusBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_WRITE)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        participant = await AffiliateService(db).set_participant_status(
            participant_id=participant_id,
            status=body.status,
            reason=body.reason,
        )
    except AffiliateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AffiliateConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.participant.status_changed",
        payload={
            "participantId": str(participant.id),
            "status": participant.status,
            "reason": body.reason,
        },
    )
    await db.commit()
    rows = await AffiliateService(db).list_participants(query=participant.email)
    return rows[0]


@router.get("/referrals", response_model=list[AffiliateReferralSummary])
async def list_affiliate_referrals(
    review_state: str | None = Query(None, alias="reviewState"),
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AffiliateReferral).order_by(AffiliateReferral.created_at.desc())
    if review_state:
        stmt = stmt.where(AffiliateReferral.review_state == review_state)
    return (await db.execute(stmt)).scalars().all()


@router.post("/referrals/{referral_id}/review", response_model=AffiliateReferralSummary)
async def review_affiliate_referral(
    referral_id: UUID,
    body: AdminAffiliateReviewBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_WRITE)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        referral = await AffiliateService(db).review_referral(
            referral_id=referral_id,
            decision=body.decision,
            reason=body.reason,
        )
    except AffiliateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.referral.reviewed",
        payload={
            "referralId": str(referral.id),
            "decision": body.decision,
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(referral)
    return referral


@router.get("/rewards", response_model=list[AffiliateRewardSummary])
async def list_affiliate_rewards(
    state_filter: str | None = Query(None, alias="state"),
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AffiliateReward).order_by(AffiliateReward.created_at.desc())
    if state_filter:
        stmt = stmt.where(AffiliateReward.state == state_filter)
    return (await db.execute(stmt)).scalars().all()


@router.post(
    "/ledger/corrections",
    response_model=AdminAffiliateCorrectionResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_affiliate_ledger_correction(
    body: AdminAffiliateCorrectionBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_WRITE)
    ),
    db: AsyncSession = Depends(get_db),
):
    participant = await lock_participant(db, body.participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participante não encontrado")
    import hashlib

    legacy_key = f"admin-correction:{actor.id}:{body.evidence_reference}"
    correction_key = f"admin-correction:{actor.id}:{hashlib.sha256(body.evidence_reference.encode()).hexdigest()}"
    prior = await db.scalar(
        select(AffiliateLedgerEntry).where(
            AffiliateLedgerEntry.idempotency_key.in_([legacy_key, correction_key])
        )
    )
    if prior:
        if (
            prior.participant_id != participant.id
            or prior.account != body.account
            or prior.amount_cents != body.amount_cents
        ):
            raise HTTPException(
                status_code=409, detail="A evidência já foi usada para outra correção"
            )
        return prior
    entry = AffiliateLedgerEntry(
        participant_id=participant.id,
        entry_type="admin_correction",
        account=body.account,
        amount_cents=body.amount_cents,
        idempotency_key=correction_key,
        metadata_json={
            "reason": body.reason,
            "evidenceReference": body.evidence_reference,
            "actorId": str(actor.id),
        },
    )
    db.add(entry)
    await db.flush()
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.ledger.corrected",
        payload={
            "entryId": str(entry.id),
            "participantId": str(participant.id),
            "account": body.account,
            "amountCents": body.amount_cents,
            "reason": body.reason,
            "evidenceReference": body.evidence_reference,
        },
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/fiscal-profiles", response_model=list[AffiliateFiscalProfileItem])
async def list_affiliate_fiscal_profiles(
    status_filter: str | None = Query(None, alias="status"),
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AffiliateFiscalProfile).order_by(
        AffiliateFiscalProfile.created_at.desc()
    )
    if status_filter:
        stmt = stmt.where(AffiliateFiscalProfile.status == status_filter)
    return (await db.execute(stmt)).scalars().all()


@router.post(
    "/fiscal-profiles/{profile_id}/approve",
    response_model=AffiliateFiscalProfileItem,
)
async def approve_affiliate_fiscal_profile(
    profile_id: UUID,
    body: AdminAffiliateFiscalApprovalBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_PAYOUT)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        profile = await AffiliatePayoutService(db).approve_fiscal_profile(
            profile_id=profile_id,
            actor=actor,
            pix_validated=body.pix_validated,
        )
    except AffiliatePayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.fiscal_profile.approved",
        payload={
            "profileId": str(profile.id),
            "participantId": str(profile.participant_id),
        },
    )
    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/payouts", response_model=list[AffiliatePayoutItem])
async def list_affiliate_payouts(
    status_filter: str | None = Query(None, alias="status"),
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AffiliatePayoutRequest).order_by(
        AffiliatePayoutRequest.requested_at.desc()
    )
    if status_filter:
        stmt = stmt.where(AffiliatePayoutRequest.status == status_filter)
    return (await db.execute(stmt)).scalars().all()


@router.get("/payout-batches", response_model=list[AffiliatePayoutBatchItem])
async def list_affiliate_payout_batches(
    _: Professional = Depends(require_admin_permission(PERMISSION_AFFILIATES_READ)),
    db: AsyncSession = Depends(get_db),
):
    return (
        (
            await db.execute(
                select(AffiliatePayoutBatch).order_by(
                    AffiliatePayoutBatch.created_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/payout-batches",
    response_model=AffiliatePayoutBatchItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_affiliate_payout_batch(
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_PAYOUT)
    ),
    db: AsyncSession = Depends(get_db),
):
    from datetime import UTC, datetime

    try:
        batch = await AffiliatePayoutService(db).create_weekly_batch(
            actor=actor, now=datetime.now(UTC)
        )
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.payout_batch.created",
        payload={"batchId": str(batch.id), "competence": batch.competence},
    )
    await db.commit()
    await db.refresh(batch)
    return batch


@router.post(
    "/payout-batches/{batch_id}/approve",
    response_model=AffiliatePayoutBatchItem,
)
async def approve_affiliate_payout_batch(
    batch_id: UUID,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_PAYOUT)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        batch = await AffiliatePayoutService(db).approve_batch(
            batch_id=batch_id,
            actor=actor,
            allow_single_operator=get_settings().affiliate_payout_single_operator_pilot,
        )
    except AffiliatePayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.payout_batch.approved",
        payload={"batchId": str(batch.id)},
    )
    await db.commit()
    await db.refresh(batch)
    return batch


@router.post("/payouts/{payout_id}/processing", response_model=AffiliatePayoutItem)
async def mark_affiliate_payout_processing(
    payout_id: UUID,
    body: AdminAffiliateTransferBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_PAYOUT)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        payout = await AffiliatePayoutService(db).reconcile_transfer(
            payout_id=payout_id,
            provider_transfer_id=body.provider_transfer_id,
        )
    except (PaymentGatewayError, PaymentGatewayConfigError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível verificar a transferência no Asaas",
        ) from exc
    except AffiliatePayoutNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.payout.processing",
        payload={
            "payoutId": str(payout.id),
            "providerTransferId": body.provider_transfer_id,
        },
    )
    await db.commit()
    await db.refresh(payout)
    return payout


@router.post("/payouts/{payout_id}/cancel", response_model=AffiliatePayoutItem)
async def cancel_affiliate_payout_reserve(
    payout_id: UUID,
    body: AdminAffiliatePayoutCancelBody,
    actor: Professional = Depends(
        require_admin_permission(PERMISSION_AFFILIATES_PAYOUT)
    ),
    db: AsyncSession = Depends(get_db),
):
    payout = await db.get(AffiliatePayoutRequest, payout_id)
    if payout is None:
        raise HTTPException(status_code=404, detail="Saque não encontrado")
    try:
        payout = await AffiliatePayoutService(db).cancel_payout(
            payout_id=payout_id,
            participant_id=payout.participant_id,
            admin_override=True,
        )
    except AffiliatePayoutConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="affiliate.payout.canceled",
        payload={
            "payoutId": str(payout.id),
            "reason": body.reason,
            "providerNotSent": body.provider_not_sent,
        },
    )
    await db.commit()
    await db.refresh(payout)
    return payout
