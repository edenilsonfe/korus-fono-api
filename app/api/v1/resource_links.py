"""F17/4.2 — vínculos de recursos com metas e programas ABA.

Contrato §3.4: GET devolve ``list[LinkedResourceResponse]``; PUT body ``{}`` é
idempotente por par de FKs; DELETE repetido continua 204. Meta usa
``clinical:read`` para ler e ``clinical:write`` + autor da meta para gerenciar;
programa usa o gate ABA existente (``require_access``).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.resource_link import LinkedResourceResponse, LinkResourceBody
from app.services.resource_link_service import (
    LINK_ERRORS,
    link_goal_resource,
    link_http_error,
    link_program_resource,
    list_goal_resources,
    list_program_resources,
    unlink_goal_resource,
    unlink_program_resource,
)

router = APIRouter(prefix="/patients/{patient_id}", tags=["resource-links"])
VerifiedProfessional = Annotated[Professional, Depends(require_verified_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/goals/{goal_id}/resources",
    response_model=list[LinkedResourceResponse],
)
async def get_goal_resources(
    patient_id: UUID,
    goal_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    try:
        return await list_goal_resources(db, patient_id, goal_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc


@router.put(
    "/goals/{goal_id}/resources/{resource_id}",
    response_model=LinkedResourceResponse,
)
async def put_goal_resource(
    patient_id: UUID,
    goal_id: UUID,
    resource_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    body: LinkResourceBody | None = None,
):
    try:
        return await link_goal_resource(db, patient_id, goal_id, resource_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc


@router.delete(
    "/goals/{goal_id}/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_goal_resource(
    patient_id: UUID,
    goal_id: UUID,
    resource_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    try:
        await unlink_goal_resource(db, patient_id, goal_id, resource_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc


@router.get(
    "/intervention-programs/{program_id}/resources",
    response_model=list[LinkedResourceResponse],
)
async def get_program_resources(
    patient_id: UUID,
    program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    try:
        return await list_program_resources(db, patient_id, program_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc


@router.put(
    "/intervention-programs/{program_id}/resources/{resource_id}",
    response_model=LinkedResourceResponse,
)
async def put_program_resource(
    patient_id: UUID,
    program_id: UUID,
    resource_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    body: LinkResourceBody | None = None,
):
    try:
        return await link_program_resource(db, patient_id, program_id, resource_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc


@router.delete(
    "/intervention-programs/{program_id}/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_program_resource(
    patient_id: UUID,
    program_id: UUID,
    resource_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    try:
        await unlink_program_resource(db, patient_id, program_id, resource_id, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc
