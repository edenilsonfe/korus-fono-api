"""F14 — administração privada do portal da família (§3.1–3.2).

Escopo exclusivo do dono do paciente (``get_patient_for_professional``);
care team não enxerga destinatários, autorizações ou links. O router é fino:
regra de negócio em ``app.services.family_portal_access``. Toda mutação faz
COMMIT explícito antes do 2xx para que o refetch imediato veja o novo estado.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.db.session import get_db
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal import (
    FamilyPortalDisableRequest,
    FamilyPortalEnableRequest,
    FamilyPortalEventResponse,
    FamilyPortalGrantIssuedResponse,
    FamilyPortalGrantIssueRequest,
    FamilyPortalGrantResponse,
    FamilyPortalRecipientResponse,
    FamilyPortalRecipientSettingsRequest,
    FamilyPortalRecipientUpsertRequest,
    FamilyPortalResponse,
    FamilyPortalWithdrawRequest,
)
from app.services import clinical_public_rate_limit, family_portal_access

router = APIRouter(
    prefix="/patients/{patient_id}/family-portal",
    tags=["family-portal"],
)

VerifiedProfessional = Annotated[Professional, Depends(require_verified_professional)]
OwnedPatient = Annotated[Patient, Depends(get_patient_for_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=FamilyPortalResponse)
async def get_family_portal(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Representação do portal; GET nunca cria portal nem autorização."""
    return await family_portal_access.get_portal(db, patient_id, professional)


@router.put("", response_model=FamilyPortalResponse)
async def enable_family_portal(
    patient_id: UUID,
    body: FamilyPortalEnableRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Habilita o portal (idempotente na versão corrente)."""
    response = await family_portal_access.enable_portal(
        db, patient_id, professional, body
    )
    await db.commit()
    return response


@router.post("/disable", response_model=FamilyPortalResponse)
async def disable_family_portal(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    body: FamilyPortalDisableRequest | None = None,
):
    """Desativa o portal e revoga todos os links (ação protetiva, idempotente)."""
    response = await family_portal_access.disable_portal(
        db, patient_id, professional
    )
    await db.commit()
    return response


@router.get("/recipients", response_model=PaginatedResponse[FamilyPortalRecipientResponse])
async def list_family_portal_recipients(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    return await family_portal_access.list_recipients(
        db, patient_id, professional, page=page, limit=limit
    )


@router.put("/recipients/{caregiver_id}", response_model=FamilyPortalRecipientResponse)
async def upsert_family_portal_recipient(
    patient_id: UUID,
    caregiver_id: UUID,
    body: FamilyPortalRecipientUpsertRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Autoriza/reativa o responsável com autorização append-only versionada."""
    response = await family_portal_access.upsert_recipient(
        db, patient_id, caregiver_id, professional, body
    )
    await db.commit()
    return response


@router.patch(
    "/recipients/{recipient_id}", response_model=FamilyPortalRecipientResponse
)
async def update_family_portal_recipient(
    patient_id: UUID,
    recipient_id: UUID,
    body: FamilyPortalRecipientSettingsRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Só a permissão de agenda; autorização e contatos não mudam aqui."""
    response = await family_portal_access.update_recipient_settings(
        db, patient_id, recipient_id, professional, body
    )
    await db.commit()
    return response


@router.delete(
    "/recipients/{recipient_id}/appointments",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disable_family_portal_recipient_appointments(
    patient_id: UUID,
    recipient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Desliga a agenda do destinatário; idempotente e permitido em read-only."""
    await family_portal_access.disable_recipient_appointments(
        db, patient_id, recipient_id, professional
    )
    await db.commit()


@router.post(
    "/recipients/{recipient_id}/withdraw",
    response_model=FamilyPortalRecipientResponse,
)
async def withdraw_family_portal_recipient(
    patient_id: UUID,
    recipient_id: UUID,
    body: FamilyPortalWithdrawRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Retirada protetiva: revoga links e tira o destinatário dos públicos."""
    response = await family_portal_access.withdraw_recipient(
        db, patient_id, recipient_id, professional, body
    )
    await db.commit()
    return response


@router.get("/events", response_model=PaginatedResponse[FamilyPortalEventResponse])
async def list_family_portal_events(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Trilha append-only de decisões; sem token, URL ou conteúdo clínico."""
    return await family_portal_access.list_events(
        db, patient_id, professional, page=page, limit=limit
    )


@router.get(
    "/recipients/{recipient_id}/grants",
    response_model=PaginatedResponse[FamilyPortalGrantResponse],
)
async def list_family_portal_grants(
    patient_id: UUID,
    recipient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Metadados dos links; sem token, hash ou URL recuperável."""
    return await family_portal_access.list_grants(
        db, patient_id, recipient_id, professional, page=page, limit=limit
    )


@router.post(
    "/recipients/{recipient_id}/grants",
    response_model=FamilyPortalGrantIssuedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_family_portal_grant(
    patient_id: UUID,
    recipient_id: UUID,
    body: FamilyPortalGrantIssueRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Emite/rotaciona o link; a URL com fragmento sai UMA vez após o commit."""
    # F14 (§3.2): emissão é criação de credencial sensível — limite fail-closed
    # por destinatário (10/min) e por dono (30/min). Revogação não passa aqui.
    clinical_public_rate_limit.enforce_family_portal_grant_issue_rate_limit(
        recipient_hash=clinical_public_rate_limit.hash_identifier(str(recipient_id)),
        owner_hash=clinical_public_rate_limit.hash_identifier(str(professional.id)),
    )
    grant, url = await family_portal_access.issue_grant(
        db, patient_id, recipient_id, professional, body
    )
    response = await family_portal_access.grant_issued_response(
        db, grant=grant, owner=professional, url=url
    )
    await db.commit()
    return response


@router.delete(
    "/recipients/{recipient_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_family_portal_grant(
    patient_id: UUID,
    recipient_id: UUID,
    grant_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Revoga o link; repetir continua 204 (idempotente no escopo)."""
    await family_portal_access.revoke_grant(
        db, patient_id, recipient_id, grant_id, professional
    )
    await db.commit()
