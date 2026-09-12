"""F14 — leitura pública do portal da família (§3.4).

Header ``X-Family-Portal-Token`` declarado OPCIONAL de propósito: ausência cai
no MESMO 410 genérico do token inválido, nunca 422. A credencial é exclusiva
deste namespace — Bearer profissional, token F16 ou token F1 não valem aqui.

Onda 1: raiz mínima (identidade + seções fixas). Onda 2: agenda projetada,
listagem e detalhe de itens publicados (allowlist §3.4). Onda 3: entrega de
arquivo autorizada (``material`` F17 / ``report`` pais F1) com revalidação
pós-I/O. Todo endpoint novo reusa o rate limit público (IP antes do token,
leitura por hash do ID do grant, teto próprio de arquivo) e os headers
públicos padronizados.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal import PublicFamilyPortalResponse
from app.schemas.family_portal_content import (
    PublicFamilyPortalAppointment,
    PublicItemDetail,
    PublicItemSummary,
)
from app.services import (
    clinical_public_rate_limit,
    family_portal_access,
    family_portal_appointments,
    family_portal_content,
    family_portal_files,
)

router = APIRouter(prefix="/family-portal", tags=["family-portal-public"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
FamilyPortalToken = Annotated[str | None, Header(alias="X-Family-Portal-Token")]


def _apply_public_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"


async def _resolve_public_read(
    db: AsyncSession, request: Request, raw_token: str | None
):
    """Rate limit + resolução do grant para as leituras públicas (§3.7).

    O teto por IP é consumido ANTES de resolver o token (malformado/ausente
    não pula o limite); o teto de leitura usa o hash do ID do grant — nunca o
    token bruto.
    """
    clinical_public_rate_limit.enforce_family_portal_ip_rate_limit(request)
    context = await family_portal_access.resolve_public_context(db, raw_token)
    clinical_public_rate_limit.enforce_family_portal_read_rate_limit(
        grant_hash=clinical_public_rate_limit.hash_identifier(
            str(context.grant.id)
        )
    )
    return context


@router.get("", response_model=PublicFamilyPortalResponse)
async def read_public_family_portal(
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_family_portal_token: FamilyPortalToken = None,
):
    """Projeção mínima da família; qualquer estado inválido vira 410 genérico."""
    context = await _resolve_public_read(db, request, x_family_portal_token)
    _apply_public_response_headers(response)
    return family_portal_access.build_public_portal_root(context)


@router.get(
    "/appointments",
    response_model=PaginatedResponse[PublicFamilyPortalAppointment],
)
async def list_public_family_portal_appointments(
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_family_portal_token: FamilyPortalToken = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Próximas sessões reais do dono; sem preço, série ou ação clínica."""
    context = await _resolve_public_read(db, request, x_family_portal_token)
    _apply_public_response_headers(response)
    return await family_portal_appointments.list_public_appointments(
        db, context, page=page, limit=limit
    )


@router.get(
    "/items", response_model=PaginatedResponse[PublicItemSummary]
)
async def list_public_family_portal_items(
    request: Request,
    response: Response,
    db: DatabaseSession,
    kind: Annotated[
        Literal[
            "session_summary", "goal", "notice", "material", "report"
        ],
        Query(),
    ],
    x_family_portal_token: FamilyPortalToken = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """Itens publicados no público vigente do destinatário do grant."""
    context = await _resolve_public_read(db, request, x_family_portal_token)
    _apply_public_response_headers(response)
    return await family_portal_content.list_published_items(
        db,
        portal=context.portal,
        recipient=context.recipient,
        kind=kind,
        page=page,
        limit=limit,
    )


@router.get(
    "/items/{item_id}",
    response_model=PublicItemDetail,
    response_model_exclude_none=True,
)
async def read_public_family_portal_item(
    request: Request,
    response: Response,
    db: DatabaseSession,
    item_id: UUID,
    x_family_portal_token: FamilyPortalToken = None,
):
    """Detalhe conforme o kind; item fora do público do grant vira 404."""
    context = await _resolve_public_read(db, request, x_family_portal_token)
    _apply_public_response_headers(response)
    return await family_portal_content.get_published_item(
        db,
        portal=context.portal,
        recipient=context.recipient,
        item_id=item_id,
    )


@router.get("/items/{item_id}/file")
async def read_public_family_portal_item_file(
    request: Request,
    db: DatabaseSession,
    item_id: UUID,
    x_family_portal_token: FamilyPortalToken = None,
):
    """Bytes autorizados (material F17 / relatório pais F1) ao destinatário.

    Consome o teto de leitura E o teto próprio de arquivo (10/min); a entrega
    revalida licença/entrega/revisão DEPOIS do I/O — nunca serve versão antiga
    nem inicia stream antes da checagem final. Kind sem arquivo -> 404;
    disponibilidade mudou -> 409 neutro; storage/render fora -> 503.
    """
    context = await _resolve_public_read(db, request, x_family_portal_token)
    clinical_public_rate_limit.enforce_family_portal_file_rate_limit(
        grant_hash=clinical_public_rate_limit.hash_identifier(
            str(context.grant.id)
        )
    )
    body, media_type, filename = await family_portal_files.load_public_item_file(
        db, raw_token=x_family_portal_token, item_id=item_id
    )
    file_response = Response(content=body, media_type=media_type)
    _apply_public_response_headers(file_response)
    file_response.headers["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )
    return file_response
