"""F14 — administração editorial privada + prévia do responsável.

Escopo exclusivo do dono do paciente (``get_patient_for_professional``);
care team não enxerga itens, revisões, fontes ou prévia. O router é fino:
regra de negócio em ``app.services.family_portal_content``. Toda mutação faz
COMMIT explícito antes do 2xx para que o refetch imediato veja o novo estado
(inclusive a publicação e a retirada).

Os cinco tipos estão habilitados (``session_summary``/``goal``/``notice`` +
``material``/``report`` desde a onda 3, com fontes ``resource``/``reportDelivery``
revalidadas na publicação); nenhum caminho devolve stub de sucesso.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_patient_for_professional,
    require_verified_professional,
)
from app.db.session import get_db
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal_content import (
    FamilyPortalItemCreateRequest,
    FamilyPortalItemPublishRequest,
    FamilyPortalItemResponse,
    FamilyPortalItemRevisionResponse,
    FamilyPortalItemSummary,
    FamilyPortalItemUpdateRequest,
    FamilyPortalItemWithdrawRequest,
    FamilyPortalSourceCandidate,
    PublicItemSummary,
)
from app.services import family_portal_content

router = APIRouter(
    prefix="/patients/{patient_id}/family-portal",
    tags=["family-portal-content"],
)

VerifiedProfessional = Annotated[
    Professional, Depends(require_verified_professional)
]
OwnedPatient = Annotated[Patient, Depends(get_patient_for_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]

SourceKindQuery = Annotated[
    Literal["session", "goal", "resource", "reportDelivery"], Query()
]
PublicItemKindQuery = Annotated[
    Literal["session_summary", "goal", "notice", "material", "report"], Query()
]


@router.get(
    "/sources",
    response_model=PaginatedResponse[FamilyPortalSourceCandidate],
)
async def list_family_portal_sources(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    kind: SourceKindQuery,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Candidatos do picker (sessão/meta nesta onda); sem notas/answers."""
    return await family_portal_content.list_sources(
        db, patient_id, professional, kind=kind, q=q, page=page, limit=limit
    )


@router.post(
    "/items",
    response_model=FamilyPortalItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_family_portal_item(
    patient_id: UUID,
    body: FamilyPortalItemCreateRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Cria o rascunho (version=1); destinatários podem ficar vazios."""
    response = await family_portal_content.create_item(
        db, patient_id, professional, body
    )
    await db.commit()
    return response


@router.get(
    "/items", response_model=PaginatedResponse[FamilyPortalItemSummary]
)
async def list_family_portal_items(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    kind: Annotated[
        Literal["session_summary", "goal", "notice", "material", "report"]
        | None,
        Query(),
    ] = None,
    status_filter: Annotated[
        Literal["draft", "published", "withdrawn"] | None,
        Query(alias="status"),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Itens do portal do dono (rascunhos, publicados e retirados)."""
    return await family_portal_content.list_items(
        db,
        patient_id,
        professional,
        kind=kind,
        status_filter=status_filter,
        page=page,
        limit=limit,
    )


@router.get("/items/{item_id}", response_model=FamilyPortalItemResponse)
async def get_family_portal_item(
    patient_id: UUID,
    item_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await family_portal_content.get_item(
        db, patient_id, professional, item_id
    )


@router.patch("/items/{item_id}", response_model=FamilyPortalItemResponse)
async def update_family_portal_item(
    patient_id: UUID,
    item_id: UUID,
    body: FamilyPortalItemUpdateRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Edita rascunho (kind/source imutáveis); mudanças reais versionam."""
    response = await family_portal_content.update_item(
        db, patient_id, professional, item_id, body
    )
    await db.commit()
    return response


@router.post(
    "/items/{item_id}/publish", response_model=FamilyPortalItemResponse
)
async def publish_family_portal_item(
    patient_id: UUID,
    item_id: UUID,
    body: FamilyPortalItemPublishRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Publica revisão imutável + público vigente na MESMA transação."""
    response = await family_portal_content.publish_item(
        db, patient_id, professional, item_id, body
    )
    await db.commit()
    return response


@router.post(
    "/items/{item_id}/withdraw", response_model=FamilyPortalItemResponse
)
async def withdraw_family_portal_item(
    patient_id: UUID,
    item_id: UUID,
    body: FamilyPortalItemWithdrawRequest,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Retira de todos imediatamente; preserva texto/revisões (protetivo)."""
    response = await family_portal_content.withdraw_item(
        db, patient_id, professional, item_id
    )
    await db.commit()
    return response


@router.delete(
    "/items/{item_id}/recipients/{recipient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_family_portal_item_recipient(
    patient_id: UUID,
    item_id: UUID,
    recipient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    """Retirada protetiva individual (público + rascunho); idempotente."""
    await family_portal_content.remove_item_recipient(
        db, patient_id, professional, item_id, recipient_id
    )
    await db.commit()


@router.get(
    "/items/{item_id}/revisions",
    response_model=PaginatedResponse[FamilyPortalItemRevisionResponse],
)
async def list_family_portal_item_revisions(
    patient_id: UUID,
    item_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Histórico privado append-only; nunca servido à família."""
    return await family_portal_content.list_item_revisions(
        db, patient_id, professional, item_id, page=page, limit=limit
    )


@router.get(
    "/preview", response_model=PaginatedResponse[PublicItemSummary]
)
async def preview_family_portal_content(
    patient_id: UUID,
    patient: OwnedPatient,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    recipient_id: Annotated[UUID, Query(alias="recipientId")],
    kind: PublicItemKindQuery,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Prévia privada: MESMO envelope do GET público para o destinatário.

    Somente conteúdo efetivamente publicado e acessível a ele; não emite
    grant/token, não amplia audiência e não serve arquivo.
    """
    return await family_portal_content.build_recipient_preview(
        db,
        patient_id,
        professional,
        recipient_id=recipient_id,
        kind=kind,
        page=page,
        limit=limit,
    )
