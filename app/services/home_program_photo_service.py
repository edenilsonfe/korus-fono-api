"""F16 — foto opcional da resposta familiar (Tarefa 5.3).

Responsabilidades:

- validação REAL da imagem (JPEG/PNG até 5 MiB e 12 megapixels): magic bytes,
  decodificação completa e REENCODIFICAÇÃO em memória, descartando EXIF/GPS e
  qualquer metadado (os pixels são copiados para uma imagem nova);
- processamento SEMPRE fora do event loop (``run_in_threadpool``): Pillow é
  CPU-bound e não pode travar o loop assíncrono;
- reserva de limpeza ANTES do upload (``reserve_storage_cleanup``), I/O sem
  lock de banco e reaquisição dos locks (paciente → programa → grant →
  check-in) com revalidação do grant/entitlement ao final: revogação no
  intervalo impede o commit e o blob recém-enviado fica na fila do janitor;
- uma foto vigente por resposta: a nova substitui a anterior NA MESMA
  transação de vínculo (a antiga vira ``deleted`` e seu blob é enfileirado);
- comandos idempotentes por ``clientRecordId`` (``HomeProgramEvent``) e
  controle otimista pela versão do check-in; a foto é INDEPENDENTE de marcar
  feito e nunca altera o texto da resposta;
- leitura dos bytes por esta API (nunca presigned público durável), tanto na
  fronteira familiar (grant) quanto na leitura clínica autenticada.

Fotos familiares NUNCA são ``Resource``: nenhum caminho daqui cria recurso,
licença, meta ou medição.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, UploadFile, status
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.resource_catalog import RESOURCE_MAX_BYTES
from app.core.utils import utcnow
from app.models.home_program import (
    HomeProgramEvent,
    HomeProgramPhoto,
)
from app.schemas.home_program import HomeProgramPhotoDeleteRequest
from app.services.attachment_upload import (
    normalize_content_type,
    sniff_content_type,
)
from app.services.entitlement_service import EntitlementService
from app.services.home_program_access import (
    PublicHomeProgramContext,
    current_photo,
    require_check_in,
    resolve_public_grant,
)
from app.services.home_program_response_service import (
    CLIENT_RECORD_REUSED_MESSAGE,
    ENTITLEMENT_BLOCKED_MESSAGE,
    STALE_VERSION_MESSAGE,
)
from app.services.storage import (
    StorageLimitExceededError,
    is_missing_object_error,
    storage_service,
)
from app.services.storage_cleanup_service import (
    queue_storage_cleanup,
    reserve_storage_cleanup,
    resolve_storage_cleanup,
)

logger = logging.getLogger(__name__)

PHOTO_UPLOADED_EVENT = "home_program_photo_uploaded"
PHOTO_DELETED_EVENT = "home_program_photo_deleted"

MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 12_000_000
PHOTO_CONTENT_TYPES: tuple[str, ...] = ("image/jpeg", "image/png")
PHOTO_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "photo.jpg",
    "image/png": "photo.png",
}
UPLOAD_READ_CHUNK_SIZE = 1024 * 1024

PHOTO_TOO_LARGE_DETAIL = "A foto excede o tamanho máximo de 5 MiB."
PHOTO_TYPE_DETAIL = "Envie uma foto JPEG ou PNG."
PHOTO_INVALID_DETAIL = "A foto enviada é inválida ou está corrompida."
PHOTO_PIXELS_DETAIL = "A foto excede o limite de 12 megapixels."
STORAGE_UNAVAILABLE_DETAIL = (
    "Armazenamento indisponível no momento. Tente novamente."
)
PHOTO_FILE_NOT_FOUND_DETAIL = "Arquivo da foto não encontrado."
PHOTO_FILE_UNAVAILABLE_DETAIL = (
    "Arquivo da foto indisponível no momento. Tente novamente."
)
MATERIAL_FILE_NOT_FOUND_DETAIL = "Arquivo do material não encontrado."
MATERIAL_FILE_UNAVAILABLE_DETAIL = (
    "Arquivo do material indisponível no momento. Tente novamente."
)

_CLEAN_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=PHOTO_TOO_LARGE_DETAIL,
    )


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ENTITLEMENT_BLOCKED_MESSAGE,
    )


def _storage_unavailable(detail: str = STORAGE_UNAVAILABLE_DETAIL) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


def _command_hash(payload: dict) -> str:
    """SHA-256 canônico do comando (sem comentário/token em claro em eventos)."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def photo_storage_key(
    *,
    patient_id: UUID,
    program_id: UUID,
    photo_id: UUID,
    content_type: str,
) -> str:
    """Chave privada imutável por operação: nunca reutiliza o nome do cliente."""
    filename = PHOTO_EXTENSIONS[content_type]
    return (
        f"patients/{patient_id}/home-programs/{program_id}/photos/"
        f"{photo_id}/{filename}"
    )


@dataclass(frozen=True)
class ProcessedPhoto:
    """Bytes reencodados (sem metadados) e tipo canônico da imagem."""

    body: bytes
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class PhotoCommandResult:
    """Resultado de um comando de foto; ``photo`` vigente (None após remoção)."""

    photo: HomeProgramPhoto | None
    has_photo: bool
    version: int


async def _require_entitlement(
    db: AsyncSession, context: PublicHomeProgramContext
) -> None:
    """Revalida ``can_write`` do dono MESMO SEM JWT (família usa token)."""
    if not await EntitlementService(db).can_write(context.owner):
        raise _forbidden()


async def _existing_event(
    db: AsyncSession, *, grant_id: UUID, client_record_id: UUID
) -> HomeProgramEvent | None:
    return await db.scalar(
        select(HomeProgramEvent).where(
            HomeProgramEvent.grant_id == grant_id,
            HomeProgramEvent.client_record_id == client_record_id,
        )
    )


async def read_upload_body(
    upload: UploadFile, max_bytes: int = MAX_PHOTO_BYTES
) -> bytes:
    """Lê o multipart em blocos; 413 quando o corpo real cruza o orçamento."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _canonical_mode(img: Image.Image) -> str:
    if img.mode in ("RGB", "RGBA", "L"):
        return img.mode
    if img.mode == "P":
        return "RGBA" if "transparency" in img.info else "RGB"
    return "RGB"


def strip_image_metadata(img: Image.Image) -> Image.Image:
    """Copia só os pixels para uma imagem nova: EXIF/GPS/texto não sobrevivem."""
    mode = _canonical_mode(img)
    source = img if img.mode == mode else img.convert(mode)
    clean = Image.new(mode, source.size)
    clean.paste(source)
    clean.info.clear()
    return clean


def process_photo_bytes(body: bytes, declared_content_type: str | None) -> ProcessedPhoto:
    """Validação real + reencodificação (síncrono; chamado fora do event loop).

    Ordem: tamanho (413) → tipo declarado/magic bytes (422) → decodificação
    real e limite de pixels (422) → reencode sem metadados.
    """
    if len(body) > MAX_PHOTO_BYTES:
        raise _too_large()
    if not body:
        raise _unprocessable(PHOTO_INVALID_DETAIL)
    declared = normalize_content_type(declared_content_type)
    if declared not in PHOTO_CONTENT_TYPES:
        raise _unprocessable(PHOTO_TYPE_DETAIL)
    if sniff_content_type(body) != declared:
        raise _unprocessable(PHOTO_TYPE_DETAIL)

    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt = img.format
            if fmt not in _CLEAN_FORMATS:
                raise _unprocessable(PHOTO_TYPE_DETAIL)
            width, height = img.size
            if width <= 0 or height <= 0:
                raise _unprocessable(PHOTO_INVALID_DETAIL)
            if width * height > MAX_PHOTO_PIXELS:
                raise _unprocessable(PHOTO_PIXELS_DETAIL)
            img.load()  # decodificação REAL: bytes corrompidos falham aqui
            clean = strip_image_metadata(img)
    except HTTPException:
        raise
    except Image.DecompressionBombError as exc:
        raise _unprocessable(PHOTO_PIXELS_DETAIL) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise _unprocessable(PHOTO_INVALID_DETAIL) from exc

    out = io.BytesIO()
    if fmt == "JPEG":
        clean.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    else:
        clean.save(out, format="PNG", optimize=True)
    rendered = out.getvalue()
    if len(rendered) > MAX_PHOTO_BYTES:
        raise _too_large()
    return ProcessedPhoto(
        body=rendered,
        content_type=_CLEAN_FORMATS[fmt],
        width=width,
        height=height,
    )


async def process_photo_upload(
    body: bytes, declared_content_type: str | None
) -> ProcessedPhoto:
    """Pillow é CPU-bound: roda em thread, nunca no event loop."""
    return await run_in_threadpool(process_photo_bytes, body, declared_content_type)


def _photo_command_hash(
    *,
    operation: str,
    check_in_id: UUID,
    expected_version: int,
    raw_sha256: str | None = None,
) -> str:
    payload: dict = {
        "operation": operation,
        "checkInId": str(check_in_id),
        "expectedVersion": expected_version,
    }
    if raw_sha256 is not None:
        payload["sha256"] = raw_sha256
    return _command_hash(payload)


async def _replay(
    db: AsyncSession,
    event: HomeProgramEvent,
    *,
    payload_hash: str,
    check_in_id: UUID,
    version: int,
) -> PhotoCommandResult:
    """Replay idêntico devolve o mesmo resultado; payload diferente vira 409."""
    if event.payload_hash != payload_hash or event.check_in_id != check_in_id:
        raise _conflict(CLIENT_RECORD_REUSED_MESSAGE)
    photo = await current_photo(db, check_in_id)
    return PhotoCommandResult(
        photo=photo, has_photo=photo is not None, version=version
    )


async def upload_check_in_photo(
    db: AsyncSession,
    *,
    raw_token: str | None,
    check_in_id: UUID,
    upload: UploadFile,
    client_record_id: UUID,
    expected_version: int,
) -> PhotoCommandResult:
    """Envia a foto da resposta: valida/reserva → I/O → revalida → vincula.

    A falha (imagem inválida, storage fora, revogação no intervalo) deixa o
    check-in textual INTACTO: nada da resposta é alterado por este comando.
    """
    raw_body = await read_upload_body(upload)

    context = await resolve_public_grant(db, raw_token)
    await _require_entitlement(db, context)
    check_in = await require_check_in(db, context.program.id, check_in_id)
    payload_hash = _photo_command_hash(
        operation=PHOTO_UPLOADED_EVENT,
        check_in_id=check_in.id,
        expected_version=expected_version,
        raw_sha256=hashlib.sha256(raw_body).hexdigest(),
    )
    event = await _existing_event(
        db, grant_id=context.grant.id, client_record_id=client_record_id
    )
    if event is not None:
        return await _replay(
            db,
            event,
            payload_hash=payload_hash,
            check_in_id=check_in.id,
            version=check_in.version,
        )
    if check_in.version != expected_version:
        raise _conflict(STALE_VERSION_MESSAGE)

    processed = await process_photo_upload(raw_body, upload.content_type)

    photo_id = uuid.uuid4()
    storage_key = photo_storage_key(
        patient_id=context.patient.id,
        program_id=context.program.id,
        photo_id=photo_id,
        content_type=processed.content_type,
    )
    # Reserva ANTES do I/O, com commit próprio: uma queda entre o upload e a
    # associação deixa o blob órfão na fila do janitor (nunca lixo silencioso).
    reservation = await reserve_storage_cleanup(
        db, storage_key, reason="home_program_photo"
    )
    try:
        await storage_service.upload(
            storage_key, processed.body, processed.content_type
        )
    except Exception as exc:  # noqa: BLE001 — vira 503; reserva permanece
        logger.exception("Falha ao enviar a foto do programa de casa")
        raise _storage_unavailable() from exc

    # Reaquisição dos locks (paciente → programa → grant → check-in) e
    # revalidação DEPOIS do I/O: revogação/retirada no intervalo vira 410 e a
    # transação não commita — o blob fica para o janitor.
    context = await resolve_public_grant(db, raw_token, lock=True)
    await _require_entitlement(db, context)
    check_in = await require_check_in(
        db, context.program.id, check_in_id, lock=True
    )
    event = await _existing_event(
        db, grant_id=context.grant.id, client_record_id=client_record_id
    )
    if event is not None:
        return await _replay(
            db,
            event,
            payload_hash=payload_hash,
            check_in_id=check_in.id,
            version=check_in.version,
        )
    if check_in.version != expected_version:
        raise _conflict(STALE_VERSION_MESSAGE)

    previous = await current_photo(db, check_in.id, lock=True)
    if previous is not None:
        previous.status = "deleted"
        if previous.storage_key:
            # O blob anterior só pode sumir depois do commit desta troca; o
            # janitor revalida que nenhuma foto vigente ainda o referencia.
            queue_storage_cleanup(
                db,
                previous.storage_key,
                reason="home_program_photo_replaced",
            )

    now = utcnow()
    photo = HomeProgramPhoto(
        id=photo_id,
        check_in_id=check_in.id,
        program_id=context.program.id,
        status="ready",
        storage_key=storage_key,
        content_type=processed.content_type,
        size_bytes=len(processed.body),
        sha256=hashlib.sha256(processed.body).hexdigest(),
    )
    db.add(photo)
    # Retirada da reserva na MESMA transação que associa o blob à foto.
    resolve_storage_cleanup(reservation)
    db.add(
        HomeProgramEvent(
            program_id=context.program.id,
            grant_id=context.grant.id,
            task_id=check_in.task_id,
            check_in_id=check_in.id,
            event_type=PHOTO_UPLOADED_EVENT,
            payload_hash=payload_hash,
            client_record_id=client_record_id,
            result_version=check_in.version,
            occurred_at=now,
        )
    )
    await db.flush()
    return PhotoCommandResult(photo=photo, has_photo=True, version=check_in.version)


async def delete_check_in_photo(
    db: AsyncSession,
    *,
    raw_token: str | None,
    check_in_id: UUID,
    body: HomeProgramPhotoDeleteRequest,
) -> PhotoCommandResult:
    """Remove a foto vigente (idempotente por request; não mexe no check-in).

    Com ``done=false``/comentário intactos: retirar a foto JAMAIS apaga a
    resposta textual, e o blob antigo vai para a fila do janitor.
    """
    context = await resolve_public_grant(db, raw_token)
    await _require_entitlement(db, context)
    check_in = await require_check_in(db, context.program.id, check_in_id)
    payload_hash = _photo_command_hash(
        operation=PHOTO_DELETED_EVENT,
        check_in_id=check_in.id,
        expected_version=body.expected_version,
    )
    event = await _existing_event(
        db, grant_id=context.grant.id, client_record_id=body.client_record_id
    )
    if event is not None:
        return await _replay(
            db,
            event,
            payload_hash=payload_hash,
            check_in_id=check_in.id,
            version=check_in.version,
        )
    if check_in.version != body.expected_version:
        raise _conflict(STALE_VERSION_MESSAGE)

    context = await resolve_public_grant(db, raw_token, lock=True)
    await _require_entitlement(db, context)
    check_in = await require_check_in(
        db, context.program.id, check_in_id, lock=True
    )
    event = await _existing_event(
        db, grant_id=context.grant.id, client_record_id=body.client_record_id
    )
    if event is not None:
        return await _replay(
            db,
            event,
            payload_hash=payload_hash,
            check_in_id=check_in.id,
            version=check_in.version,
        )
    if check_in.version != body.expected_version:
        raise _conflict(STALE_VERSION_MESSAGE)

    previous = await current_photo(db, check_in.id, lock=True)
    if previous is not None:
        previous.status = "deleted"
        if previous.storage_key:
            queue_storage_cleanup(
                db,
                previous.storage_key,
                reason="home_program_photo_deleted",
            )

    now = utcnow()
    db.add(
        HomeProgramEvent(
            program_id=context.program.id,
            grant_id=context.grant.id,
            task_id=check_in.task_id,
            check_in_id=check_in.id,
            event_type=PHOTO_DELETED_EVENT,
            payload_hash=payload_hash,
            client_record_id=body.client_record_id,
            result_version=check_in.version,
            occurred_at=now,
        )
    )
    await db.flush()
    return PhotoCommandResult(photo=None, has_photo=False, version=check_in.version)


def _download_or_http(
    exc: Exception, *, not_found_detail: str, unavailable_detail: str
) -> HTTPException:
    if is_missing_object_error(exc):
        return _not_found(not_found_detail)
    return _storage_unavailable(unavailable_detail)


async def load_photo_bytes(photo: HomeProgramPhoto) -> tuple[bytes, str]:
    """Bytes da foto vigente pela API (sem presigned público durável)."""
    if not photo.storage_key:
        raise _not_found(PHOTO_FILE_NOT_FOUND_DETAIL)
    try:
        body, _ = await storage_service.download_limited(
            photo.storage_key, max_bytes=MAX_PHOTO_BYTES
        )
    except StorageLimitExceededError as exc:
        raise _storage_unavailable(PHOTO_FILE_UNAVAILABLE_DETAIL) from exc
    except Exception as exc:  # noqa: BLE001 — ausente vira 404; resto vira 503
        logger.exception("Falha ao carregar a foto do programa de casa")
        raise _download_or_http(
            exc,
            not_found_detail=PHOTO_FILE_NOT_FOUND_DETAIL,
            unavailable_detail=PHOTO_FILE_UNAVAILABLE_DETAIL,
        ) from exc
    return body, photo.content_type or "application/octet-stream"


async def load_material_bytes(resource) -> tuple[bytes, str]:
    """Bytes do material fixado na tarefa, revalidado pelo gate da Tarefa 5.3."""
    if not resource.storage_key:
        raise _not_found(MATERIAL_FILE_NOT_FOUND_DETAIL)
    try:
        body, _ = await storage_service.download_limited(
            resource.storage_key, max_bytes=RESOURCE_MAX_BYTES
        )
    except StorageLimitExceededError as exc:
        raise _storage_unavailable(MATERIAL_FILE_UNAVAILABLE_DETAIL) from exc
    except Exception as exc:  # noqa: BLE001 — ausente vira 404; resto vira 503
        logger.exception("Falha ao carregar o material do programa de casa")
        raise _download_or_http(
            exc,
            not_found_detail=MATERIAL_FILE_NOT_FOUND_DETAIL,
            unavailable_detail=MATERIAL_FILE_UNAVAILABLE_DETAIL,
        ) from exc
    return body, resource.content_type or "application/octet-stream"
