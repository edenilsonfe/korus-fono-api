from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.home_program import (
    HomeProgramArchiveRequest,
    HomeProgramCheckInSummaryResponse,
    HomeProgramCreate,
    HomeProgramGrantCreate,
    HomeProgramGrantResponse,
    HomeProgramPublishRequest,
    HomeProgramResponse,
    HomeProgramUpdate,
)
from app.services import (
    home_program_access,
    home_program_photo_service,
    home_program_service,
)

router = APIRouter(
    prefix="/patients/{patient_id}/home-programs",
    tags=["home-programs"],
)
VerifiedProfessional = Annotated[Professional, Depends(require_verified_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=PaginatedResponse[HomeProgramResponse])
async def list_home_programs(
    patient_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await home_program_service.list_programs(
        db, patient_id, professional, page=page, limit=limit
    )


@router.post(
    "",
    response_model=HomeProgramResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_home_program(
    patient_id: UUID,
    body: HomeProgramCreate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await home_program_service.create_program(
        db, patient_id, professional, body
    )


@router.get("/{home_program_id}", response_model=HomeProgramResponse)
async def get_home_program(
    patient_id: UUID,
    home_program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await home_program_service.get_program(
        db, patient_id, home_program_id, professional
    )


@router.patch("/{home_program_id}", response_model=HomeProgramResponse)
async def update_home_program(
    patient_id: UUID,
    home_program_id: UUID,
    body: HomeProgramUpdate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await home_program_service.update_program(
        db, patient_id, home_program_id, professional, body
    )


@router.post("/{home_program_id}/publish", response_model=HomeProgramResponse)
async def publish_home_program(
    patient_id: UUID,
    home_program_id: UUID,
    body: HomeProgramPublishRequest,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await home_program_service.publish_program(
        db, patient_id, home_program_id, professional, body
    )


@router.post("/{home_program_id}/archive", response_model=HomeProgramResponse)
async def archive_home_program(
    patient_id: UUID,
    home_program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    body: HomeProgramArchiveRequest | None = None,
):
    """Arquiva o programa (idempotente) e revoga todos os grants ativos."""
    return await home_program_service.archive_program(
        db, patient_id, home_program_id, professional
    )


@router.get("/{home_program_id}/grants", response_model=list[HomeProgramGrantResponse])
async def list_home_program_grants(
    patient_id: UUID,
    home_program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await home_program_access.list_grants(
        db, patient_id, home_program_id, professional
    )


@router.post(
    "/{home_program_id}/grants",
    response_model=HomeProgramGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_home_program_grant(
    patient_id: UUID,
    home_program_id: UUID,
    body: HomeProgramGrantCreate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    grant, url = await home_program_access.create_grant(
        db, patient_id, home_program_id, professional, body
    )
    return home_program_access.grant_response(grant, url=url)


@router.delete(
    "/{home_program_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_home_program_grant(
    patient_id: UUID,
    home_program_id: UUID,
    grant_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    await home_program_access.revoke_grant(
        db, patient_id, home_program_id, grant_id, professional
    )


@router.get(
    "/{home_program_id}/check-ins",
    response_model=PaginatedResponse[HomeProgramCheckInSummaryResponse],
)
async def list_home_program_check_ins(
    patient_id: UUID,
    home_program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Respostas autodeclaradas da família (leitura clínica autorizada).

    Sem contatos nem nome do responsável fora do escopo do dono; a presença de
    foto é apenas um sinalizador (os bytes saem pela rota de arquivo).
    """
    return await home_program_service.list_check_ins(
        db, patient_id, home_program_id, professional, page=page, limit=limit
    )


@router.get("/{home_program_id}/check-ins/{check_in_id}/photo/file")
async def read_home_program_check_in_photo(
    patient_id: UUID,
    home_program_id: UUID,
    check_in_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Bytes da foto vigente para a ACL clínica atual (nunca presigned durável)."""
    photo = await home_program_service.professional_check_in_photo(
        db, patient_id, home_program_id, check_in_id, professional
    )
    body, content_type = await home_program_photo_service.load_photo_bytes(photo)
    filename = home_program_photo_service.PHOTO_EXTENSIONS.get(
        content_type, "photo.jpg"
    )
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )
