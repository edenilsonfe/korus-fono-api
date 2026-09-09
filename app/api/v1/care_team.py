from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.care_team import (
    CareTeamInvitationCreate,
    CareTeamInvitationToken,
    CareTeamMemberResponse,
    CareTeamMemberUpdate,
    PatientAccessEventResponse,
    SharingConsentCreate,
    SharingConsentResponse,
)
from app.schemas.common import PaginatedResponse
from app.services.care_team_service import (
    accept_invitation,
    consent_response,
    decline_invitation,
    invite_member,
    list_access_events,
    list_team,
    member_response,
    record_consent,
    resend_invitation,
    revoke_member,
    send_invitation_email_sync,
    update_member_role,
)

router = APIRouter(tags=["care-team"])


@router.get(
    "/patients/{patient_id}/care-team",
    response_model=list[CareTeamMemberResponse],
)
async def get_care_team(
    patient_id: UUID,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await list_team(db, patient_id, actor)


@router.get(
    "/patients/{patient_id}/access-events",
    response_model=PaginatedResponse[PatientAccessEventResponse],
)
async def get_patient_access_events(
    patient_id: UUID,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    items, total = await list_access_events(
        db, patient_id, actor, page=page, limit=limit
    )
    return PaginatedResponse(items=items, total=total, page=page, limit=limit)


@router.post(
    "/patients/{patient_id}/sharing-consents",
    response_model=SharingConsentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sharing_consent(
    patient_id: UUID,
    body: SharingConsentCreate,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return consent_response(await record_consent(db, patient_id, actor, body))


@router.post(
    "/patients/{patient_id}/care-team/invitations",
    response_model=CareTeamMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_care_team_invitation(
    patient_id: UUID,
    body: CareTeamInvitationCreate,
    background_tasks: BackgroundTasks,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    member, invited, raw_token = await invite_member(
        db,
        patient_id,
        actor,
        email=str(body.email),
        role=body.role,
    )
    background_tasks.add_task(
        send_invitation_email_sync,
        invited.email,
        invited.name,
        actor.name,
        raw_token,
    )
    return member_response(member, invited)


@router.post(
    "/patients/{patient_id}/care-team/invitations/{member_id}/resend",
    response_model=CareTeamMemberResponse,
)
async def resend_care_team_invitation(
    patient_id: UUID,
    member_id: UUID,
    background_tasks: BackgroundTasks,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    member, invited, raw_token = await resend_invitation(
        db, patient_id, member_id, actor
    )
    background_tasks.add_task(
        send_invitation_email_sync,
        invited.email,
        invited.name,
        actor.name,
        raw_token,
    )
    return member_response(member, invited)


@router.patch(
    "/patients/{patient_id}/care-team/{member_id}",
    response_model=CareTeamMemberResponse,
)
async def patch_care_team_member(
    patient_id: UUID,
    member_id: UUID,
    body: CareTeamMemberUpdate,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    member, professional = await update_member_role(
        db, patient_id, member_id, actor, body.role
    )
    return member_response(member, professional)


@router.delete(
    "/patients/{patient_id}/care-team/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_care_team_member(
    patient_id: UUID,
    member_id: UUID,
    reason: str | None = Query(None, max_length=500),
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await revoke_member(db, patient_id, member_id, actor, reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/care-team/invitations/accept",
    response_model=CareTeamMemberResponse,
)
async def accept_care_team_invitation(
    body: CareTeamInvitationToken,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    member = await accept_invitation(db, actor, body.token)
    return member_response(member, actor)


@router.post(
    "/care-team/invitations/decline",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def decline_care_team_invitation(
    body: CareTeamInvitationToken,
    actor: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await decline_invitation(db, actor, body.token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
