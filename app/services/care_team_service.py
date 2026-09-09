import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.care_team import (
    PatientAccessEvent,
    PatientCareTeamMember,
    PatientSharingConsentEvent,
)
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.care_team import (
    CareTeamMemberResponse,
    PatientAccessEventResponse,
    SharingConsentCreate,
    SharingConsentResponse,
)
from app.services.email.resend_client import send_email
from app.services.email.templates import care_team_invitation_email
from app.services.patient_access import (
    has_permission,
    resolve_clinical_patient_access,
    resolve_patient_access,
)
from app.utils.token_hash import hash_token

INVITE_VALIDITY = timedelta(days=7)
INVITE_COOLDOWN = timedelta(minutes=10)
MAX_OPEN_INVITATIONS = 10
logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado"
    )


async def require_access(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    permission: str,
):
    access = await resolve_patient_access(db, patient_id, actor)
    if access is None or not has_permission(access, permission):
        raise _not_found()
    return access


async def require_clinical_access(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    permission: str = "clinical:read",
):
    access = await resolve_clinical_patient_access(db, patient_id, actor)
    if access is None or not has_permission(access, permission):
        raise _not_found()
    return access


async def latest_consent(
    db: AsyncSession, patient_id: UUID
) -> PatientSharingConsentEvent | None:
    return await db.scalar(
        select(PatientSharingConsentEvent)
        .where(PatientSharingConsentEvent.patient_id == patient_id)
        .order_by(
            PatientSharingConsentEvent.recorded_at.desc(),
            PatientSharingConsentEvent.id.desc(),
        )
        .limit(1)
    )


def record_access_event(
    db: AsyncSession,
    *,
    patient_id: UUID,
    actor: Professional,
    actor_role: str,
    action: str,
    resource_type: str,
    resource_id: UUID | None = None,
) -> None:
    db.add(
        PatientAccessEvent(
            patient_id=patient_id,
            actor_professional_id=actor.id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            occurred_at=utcnow(),
        )
    )


async def record_consent(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    body: SharingConsentCreate,
) -> PatientSharingConsentEvent:
    access = await require_access(db, patient_id, actor, "care_team:manage")
    if body.caregiver_id is not None:
        caregiver = await db.scalar(
            select(Caregiver).where(
                Caregiver.id == body.caregiver_id,
                Caregiver.patient_id == patient_id,
            )
        )
        if caregiver is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Responsável não pertence ao paciente",
            )

    event = PatientSharingConsentEvent(
        patient_id=patient_id,
        caregiver_id=body.caregiver_id,
        decision=body.decision,
        policy_version=body.policy_version.strip(),
        recorded_by_professional_id=actor.id,
        recorded_at=utcnow(),
        notes=body.notes.strip() if body.notes else None,
    )
    db.add(event)
    await db.flush()

    if body.decision == "withdrawn":
        members = (
            (
                await db.execute(
                    select(PatientCareTeamMember).where(
                        PatientCareTeamMember.patient_id == patient_id,
                        PatientCareTeamMember.status.in_(("invited", "active")),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = utcnow()
        for member in members:
            member.status = "revoked"
            member.invite_token_hash = None
            member.invite_expires_at = None
            member.revoked_at = now
            member.revoked_by_professional_id = actor.id
            member.revocation_reason = "Autorização de compartilhamento retirada"
            record_access_event(
                db,
                patient_id=patient_id,
                actor=actor,
                actor_role=access.role,
                action="member_revoked",
                resource_type="care_team_member",
                resource_id=member.id,
            )

    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action=f"sharing_consent_{body.decision}",
        resource_type="sharing_consent",
        resource_id=event.id,
    )
    await db.flush()
    return event


async def invite_member(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    email: str,
    role: str,
) -> tuple[PatientCareTeamMember, Professional, str]:
    access = await require_access(db, patient_id, actor, "care_team:manage")
    consent = await latest_consent(db, patient_id)
    if consent is None or consent.decision != "granted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registre a autorização de compartilhamento antes de convidar",
        )

    normalized_email = email.strip().lower()
    invited = await db.scalar(
        select(Professional).where(func.lower(Professional.email) == normalized_email)
    )
    if invited is None or invited.is_disabled or invited.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O convite exige uma conta Korus ativa com e-mail verificado",
        )
    if invited.id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O coordenador já faz parte da equipe",
        )

    now = utcnow()
    open_count = await db.scalar(
        select(func.count())
        .select_from(PatientCareTeamMember)
        .where(
            PatientCareTeamMember.patient_id == patient_id,
            PatientCareTeamMember.status.in_(("invited", "active")),
        )
    )
    existing = await db.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.patient_id == patient_id,
            PatientCareTeamMember.professional_id == invited.id,
        )
    )
    if (
        existing
        and existing.status == "invited"
        and existing.invite_expires_at is not None
        and _utc(existing.invite_expires_at) <= now
    ):
        existing.status = "expired"
        existing.invite_token_hash = None
        existing.invite_expires_at = None
    if existing and existing.status in {"invited", "active"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este profissional já possui convite ou acesso ao paciente",
        )
    if existing and _utc(existing.invited_at) > now - INVITE_COOLDOWN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Aguarde antes de convidar este profissional novamente",
        )
    if (open_count or 0) >= MAX_OPEN_INVITATIONS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Limite de integrantes da equipe assistencial atingido",
        )

    raw_token = secrets.token_urlsafe(32)
    values = {
        "role": role,
        "status": "invited",
        "invited_by_professional_id": actor.id,
        "consent_event_id": consent.id,
        "invite_token_hash": hash_token(raw_token),
        "invite_expires_at": now + INVITE_VALIDITY,
        "invited_at": now,
        "accepted_at": None,
        "revoked_at": None,
        "revoked_by_professional_id": None,
        "revocation_reason": None,
    }
    if existing is None:
        member = PatientCareTeamMember(
            patient_id=patient_id,
            professional_id=invited.id,
            **values,
        )
        db.add(member)
    else:
        member = existing
        for field, value in values.items():
            setattr(member, field, value)

    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="member_invited",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()
    return member, invited, raw_token


async def accept_invitation(
    db: AsyncSession,
    actor: Professional,
    raw_token: str,
) -> PatientCareTeamMember:
    now = utcnow()
    member = await db.scalar(
        select(PatientCareTeamMember)
        .where(
            PatientCareTeamMember.invite_token_hash == hash_token(raw_token),
            PatientCareTeamMember.status == "invited",
            PatientCareTeamMember.invite_expires_at > now,
        )
        .with_for_update()
    )
    if member is None or member.professional_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Convite inválido ou expirado",
        )
    access = await resolve_patient_access(db, member.patient_id, actor)
    if access is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Convite inválido ou expirado",
        )
    patient = await db.get(Patient, member.patient_id)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Convite inválido ou expirado",
        )
    owner = await db.get(Professional, patient.professional_id)
    owner_access = (
        await resolve_patient_access(db, patient.id, owner)
        if owner is not None
        else None
    )
    consent = await latest_consent(db, member.patient_id)
    if (
        owner_access is None
        or consent is None
        or consent.decision != "granted"
        or consent.id != member.consent_event_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Convite inválido ou expirado",
        )

    member.status = "active"
    member.accepted_at = now
    member.invite_token_hash = None
    member.invite_expires_at = None
    record_access_event(
        db,
        patient_id=member.patient_id,
        actor=actor,
        actor_role=member.role,
        action="member_accepted",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()
    return member


async def decline_invitation(
    db: AsyncSession,
    actor: Professional,
    raw_token: str,
) -> None:
    now = utcnow()
    member = await db.scalar(
        select(PatientCareTeamMember)
        .where(
            PatientCareTeamMember.invite_token_hash == hash_token(raw_token),
            PatientCareTeamMember.status == "invited",
            PatientCareTeamMember.invite_expires_at > now,
        )
        .with_for_update()
    )
    if member is None or member.professional_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Convite inválido ou expirado",
        )
    member.status = "declined"
    member.invite_token_hash = None
    member.invite_expires_at = None
    record_access_event(
        db,
        patient_id=member.patient_id,
        actor=actor,
        actor_role=member.role,
        action="member_declined",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()


async def resend_invitation(
    db: AsyncSession,
    patient_id: UUID,
    member_id: UUID,
    actor: Professional,
) -> tuple[PatientCareTeamMember, Professional, str]:
    access = await require_access(db, patient_id, actor, "care_team:manage")
    member = await db.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.id == member_id,
            PatientCareTeamMember.patient_id == patient_id,
            PatientCareTeamMember.status == "invited",
        )
    )
    if member is None:
        raise _not_found()
    now = utcnow()
    if _utc(member.invited_at) > now - INVITE_COOLDOWN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Aguarde antes de reenviar este convite",
        )
    consent = await latest_consent(db, patient_id)
    if consent is None or consent.decision != "granted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registre a autorização de compartilhamento antes de convidar",
        )
    professional = await db.get(Professional, member.professional_id)
    if (
        professional is None
        or professional.is_disabled
        or professional.email_verified_at is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A conta convidada não está disponível",
        )
    raw_token = secrets.token_urlsafe(32)
    member.consent_event_id = consent.id
    member.invite_token_hash = hash_token(raw_token)
    member.invite_expires_at = now + INVITE_VALIDITY
    member.invited_at = now
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="member_invitation_resent",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()
    return member, professional, raw_token


async def list_team(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
) -> list[CareTeamMemberResponse]:
    access = await require_access(db, patient_id, actor, "care_team:read")
    owner = await db.get(Professional, access.patient.professional_id)
    members = (
        await db.execute(
            select(PatientCareTeamMember, Professional)
            .join(
                Professional, Professional.id == PatientCareTeamMember.professional_id
            )
            .where(PatientCareTeamMember.patient_id == patient_id)
        )
    ).all()
    result = [
        CareTeamMemberResponse(
            id=None,
            patient_id=str(patient_id),
            professional_id=str(owner.id),
            professional_name=owner.name,
            specialty=owner.specialty,
            council=owner.council,
            role="coordinator",
            status="active",
        )
    ]
    role_order = {"supervisor": 0, "practitioner": 1}
    for member, professional in sorted(
        members, key=lambda item: (role_order[item[0].role], item[1].name.lower())
    ):
        result.append(member_response(member, professional))
    return result


def member_response(
    member: PatientCareTeamMember,
    professional: Professional,
) -> CareTeamMemberResponse:
    return CareTeamMemberResponse(
        id=str(member.id),
        patient_id=str(member.patient_id),
        professional_id=str(professional.id),
        professional_name=professional.name,
        specialty=professional.specialty,
        council=professional.council,
        role=member.role,
        status=member.status,
        invited_at=member.invited_at,
        accepted_at=member.accepted_at,
    )


async def list_access_events(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> tuple[list[PatientAccessEventResponse], int]:
    await require_access(db, patient_id, actor, "care_team:manage")
    total = await db.scalar(
        select(func.count())
        .select_from(PatientAccessEvent)
        .where(PatientAccessEvent.patient_id == patient_id)
    )
    events = (
        (
            await db.execute(
                select(PatientAccessEvent)
                .where(PatientAccessEvent.patient_id == patient_id)
                .order_by(
                    PatientAccessEvent.occurred_at.desc(), PatientAccessEvent.id.desc()
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return (
        [
            PatientAccessEventResponse(
                id=str(event.id),
                patient_id=str(event.patient_id),
                actor_professional_id=str(event.actor_professional_id),
                actor_role=event.actor_role,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=str(event.resource_id) if event.resource_id else None,
                occurred_at=event.occurred_at,
            )
            for event in events
        ],
        total or 0,
    )


async def update_member_role(
    db: AsyncSession,
    patient_id: UUID,
    member_id: UUID,
    actor: Professional,
    role: str,
) -> tuple[PatientCareTeamMember, Professional]:
    access = await require_access(db, patient_id, actor, "care_team:manage")
    member = await db.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.id == member_id,
            PatientCareTeamMember.patient_id == patient_id,
            PatientCareTeamMember.status.in_(("invited", "active")),
        )
    )
    if member is None:
        raise _not_found()
    member.role = role
    professional = await db.get(Professional, member.professional_id)
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="member_role_updated",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()
    return member, professional


async def revoke_member(
    db: AsyncSession,
    patient_id: UUID,
    member_id: UUID,
    actor: Professional,
    reason: str | None,
) -> None:
    access = await require_access(db, patient_id, actor, "care_team:manage")
    member = await db.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.id == member_id,
            PatientCareTeamMember.patient_id == patient_id,
            PatientCareTeamMember.status.in_(("invited", "active")),
        )
    )
    if member is None:
        raise _not_found()
    member.status = "revoked"
    member.invite_token_hash = None
    member.invite_expires_at = None
    member.revoked_at = utcnow()
    member.revoked_by_professional_id = actor.id
    member.revocation_reason = reason.strip() if reason else None
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="member_revoked",
        resource_type="care_team_member",
        resource_id=member.id,
    )
    await db.flush()


def consent_response(event: PatientSharingConsentEvent) -> SharingConsentResponse:
    return SharingConsentResponse(
        id=str(event.id),
        patient_id=str(event.patient_id),
        caregiver_id=str(event.caregiver_id) if event.caregiver_id else None,
        decision=event.decision,
        policy_version=event.policy_version,
        recorded_by_professional_id=str(event.recorded_by_professional_id),
        recorded_at=event.recorded_at,
        notes=event.notes,
    )


def send_invitation_email_sync(
    to_email: str,
    recipient_name: str,
    inviter_name: str,
    raw_token: str,
) -> None:
    settings = get_settings()
    url = (
        f"{settings.billing_frontend_base_url}/care-team/invitations"
        f"?token={quote_plus(raw_token)}"
    )
    rendered = care_team_invitation_email(
        recipient_name=recipient_name,
        inviter_name=inviter_name,
        invitation_url=url,
        expires_days=INVITE_VALIDITY.days,
    )
    try:
        send_email(
            to_email=to_email,
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
        )
    except Exception:
        logger.exception("Falha ao enviar convite de equipe assistencial")
