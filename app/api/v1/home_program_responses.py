"""F16 — fronteira pública do programa de casa (família, sem JWT).

Autenticação é o header ``X-Home-Program-Token`` (token opaco do grant; nunca
cookie, query string ou JWT profissional). Este router NÃO dá acesso a nenhum
endpoint clínico: expõe só a projeção mínima da família, o check-in atual e as
edições com controle de versão.

Rate limit público (Tarefa 5.2): 120 solicitações/min por IP confiável,
60 leituras/min por grant e 30 escritas/min por grant; o identificador do grant
vai hasheado (token bruto nunca vira chave de Redis). Mutação pública falha
fechado (503) sem o contador. O entitlement do DONO é revalidado no serviço a
cada leitura/escrita, mesmo sem JWT.
"""

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.schemas.home_program import (
    HomeProgramCheckInCreate,
    HomeProgramCheckInResponse,
    HomeProgramCheckInUpdate,
    HomeProgramPhotoDeleteRequest,
    HomeProgramPhotoResponse,
    PublicHomeProgramResponse,
)
from app.services import (
    clinical_public_rate_limit,
    home_program_access,
    home_program_photo_service,
    home_program_response_service,
)
from app.services.storage import safe_content_disposition_filename

router = APIRouter(prefix="/home-program-responses", tags=["home-program-responses"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
HomeProgramToken = Annotated[
    str | None, Header(alias="X-Home-Program-Token")
]


def _apply_public_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _public_file_headers(*, filename: str) -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{filename}"',
    }


def _grant_rate_limit_hash(grant_id: UUID) -> str:
    return clinical_public_rate_limit.hash_identifier(str(grant_id))


@router.get("", response_model=PublicHomeProgramResponse)
async def read_public_home_program(
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Programa de casa da família; GET válido continua mesmo em read-only."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_read_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    _apply_public_response_headers(response)
    return await home_program_response_service.build_public_response(db, context)


@router.post(
    "/tasks/{task_id}/check-ins",
    response_model=HomeProgramCheckInResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_public_check_in(
    task_id: UUID,
    body: HomeProgramCheckInCreate,
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Cria a resposta atual da tarefa; replay idêntico devolve 200."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_write_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    result = await home_program_response_service.create_check_in(
        db,
        raw_token=x_home_program_token,
        task_id=task_id,
        body=body,
    )
    await db.commit()
    _apply_public_response_headers(response)
    response.status_code = (
        status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )
    return result.check_in


@router.patch(
    "/check-ins/{check_in_id}",
    response_model=HomeProgramCheckInResponse,
)
async def update_public_check_in(
    check_in_id: UUID,
    body: HomeProgramCheckInUpdate,
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Edita a resposta com versão esperada; revisão anterior preservada."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_write_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    result = await home_program_response_service.update_check_in(
        db,
        raw_token=x_home_program_token,
        check_in_id=check_in_id,
        body=body,
    )
    await db.commit()
    _apply_public_response_headers(response)
    return result.check_in


@router.put(
    "/check-ins/{check_in_id}/photo",
    response_model=HomeProgramPhotoResponse,
)
async def put_public_check_in_photo(
    check_in_id: UUID,
    request: Request,
    response: Response,
    db: DatabaseSession,
    file: Annotated[UploadFile, File()],
    client_record_id: Annotated[UUID, Form(alias="clientRecordId")],
    expected_version: Annotated[int, Form(alias="expectedVersion", ge=1)],
    x_home_program_token: HomeProgramToken = None,
):
    """Envia a foto da resposta (JPEG/PNG) — independente de marcar feito.

    Multipart: ``file``, ``clientRecordId`` e ``expectedVersion`` (versão do
    check-in). Limite próprio de 10 envios/10 min por grant; a foto antiga é
    substituída na MESMA transação e seu blob vai para a limpeza F17.
    """
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_upload_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    result = await home_program_photo_service.upload_check_in_photo(
        db,
        raw_token=x_home_program_token,
        check_in_id=check_in_id,
        upload=file,
        client_record_id=client_record_id,
        expected_version=expected_version,
    )
    await db.commit()
    _apply_public_response_headers(response)
    return HomeProgramPhotoResponse(
        id=str(result.photo.id) if result.photo is not None else None,
        has_photo=result.has_photo,
        version=result.version,
    )


@router.delete(
    "/check-ins/{check_in_id}/photo",
    response_model=HomeProgramPhotoResponse,
    response_model_exclude_none=True,
)
async def delete_public_check_in_photo(
    check_in_id: UUID,
    body: HomeProgramPhotoDeleteRequest,
    request: Request,
    response: Response,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Remove a foto vigente; idempotente por request, nunca toca no texto."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_write_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    result = await home_program_photo_service.delete_check_in_photo(
        db,
        raw_token=x_home_program_token,
        check_in_id=check_in_id,
        body=body,
    )
    await db.commit()
    _apply_public_response_headers(response)
    return HomeProgramPhotoResponse(
        id=None, has_photo=result.has_photo, version=result.version
    )


@router.get("/check-ins/{check_in_id}/photo/file")
async def read_public_check_in_photo_file(
    check_in_id: UUID,
    request: Request,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Bytes da foto vigente para a família (sem presigned público durável)."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_read_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    photo = await home_program_access.require_current_photo(
        db, context.program.id, check_in_id
    )
    body, content_type = await home_program_photo_service.load_photo_bytes(photo)
    filename = home_program_photo_service.PHOTO_EXTENSIONS.get(
        content_type, "photo.jpg"
    )
    return Response(
        content=body,
        media_type=content_type,
        headers=_public_file_headers(filename=filename),
    )


@router.get("/tasks/{task_id}/materials/{resource_id}/file")
async def read_public_material_file(
    task_id: UUID,
    resource_id: UUID,
    request: Request,
    db: DatabaseSession,
    x_home_program_token: HomeProgramToken = None,
):
    """Bytes do material vinculado, com licença revalidada a CADA entrega."""
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit,
        request,
    )
    context = await home_program_access.resolve_public_grant(
        db, x_home_program_token
    )
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_home_program_response_read_rate_limit,
        grant_hash=_grant_rate_limit_hash(context.grant.id),
    )
    resource = await home_program_access.require_public_material(
        db, context, task_id, resource_id
    )
    body, content_type = await home_program_photo_service.load_material_bytes(
        resource
    )
    filename = safe_content_disposition_filename(resource.storage_key, resource.title)
    return Response(
        content=body,
        media_type=content_type,
        headers=_public_file_headers(filename=filename),
    )
