"""F14 onda 3 — entrega de arquivos autorizados do portal da família.

Responsabilidades (§3.4–3.5 do plano):

- ``material``: bytes do ``Resource`` (F17) entregues SOMENTE com licença
  familiar vigente e SHA-256 dos bytes igual ao congelado na revisão; o
  download usa teto real de bytes (20 MiB) e o objeto é lido por esta API —
  nunca presigned durável nem URL fornecida pelo cliente;
- ``report``: PDF renderizado pelo renderer F1 a partir do SNAPSHOT F1 da
  entrega (helper estrito de ``report_delivery_service``) + identidade F2 do
  profissional; o relatório ATUAL nunca substitui o snapshot;
- revalidação pós-I/O (§3.5 "Retirada durante I/O"): captura sem locks, faz o
  I/O limitado e SÓ então relê grant/audiência/revisão/licença/entrega com
  ``populate_existing``; qualquer mudança descarta os bytes e responde
  410/404/409 sem payload clínico;
- o prazo efetivo do documento é o menor entre a entrega F1 e o grant F14; a
  ausência física do objeto vira 409 neutro (disponibilidade mudou) e falha
  operacional de storage/render vira 503 — nunca uma versão antiga silenciosa.

O token continua exclusivo do header; nada aqui emite/recupera token, grava
métrica F1/F20 (recibo/visualização) ou cria segunda fila de cleanup — as
chaves seguem protegidas pelo próprio ``Resource`` no janitor.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.resource_catalog import RESOURCE_MAX_BYTES
from app.models.family_portal import FamilyPortal
from app.models.family_portal_content import (
    FamilyPortalItem,
    FamilyPortalItemRevision,
)
from app.models.resource import Resource
from app.services import (
    family_portal_access,
    family_portal_content,
    report_delivery_service,
)
from app.services.professional_branding import build_document_identity
from app.services.report_export import export_report
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
    get_current_license,
)
from app.services.storage import (
    StorageLimitExceededError,
    is_missing_object_error,
    storage_service,
)

logger = logging.getLogger(__name__)

# §3.5 — teto real de bytes do arquivo de material entregue à família.
MATERIAL_MAX_BYTES = RESOURCE_MAX_BYTES

MATERIAL_MEDIA_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
}
REPORT_MEDIA_TYPE = "application/pdf"

# §3.4 — 409 neutro: nunca expõe titular, licença, entrega ou provedor.
CONTENT_UNAVAILABLE_DETAIL = "Este conteúdo não está disponível no momento."
MATERIAL_FILE_UNAVAILABLE_DETAIL = (
    "Arquivo do material indisponível no momento. Tente novamente."
)
REPORT_FILE_UNAVAILABLE_DETAIL = (
    "Relatório indisponível no momento. Tente novamente."
)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str = CONTENT_UNAVAILABLE_DETAIL) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _service_unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


@dataclass(frozen=True)
class PreparedItemFile:
    """Arquivo preparado (pré-I/O) com os valores congelados da revalidação.

    Só guarda valores planos (ids/hashes/texto) além do contexto de render do
    relatório — a revalidação usa estes campos, nunca cópias do identity map.
    """

    kind: str
    item_id: UUID
    revision_version: int
    media_type: str
    filename: str
    frozen_sha256: str | None = None
    resource_id: UUID | None = None
    resource: Resource | None = None
    delivery_id: UUID | None = None
    delivery_context: report_delivery_service.PublicDeliveryContext | None = None
    document: report_delivery_service.PublicDocument | None = None


async def _prepare_material(
    db: AsyncSession,
    portal: FamilyPortal,
    item: FamilyPortalItem,
    revision: FamilyPortalItemRevision,
) -> PreparedItemFile:
    """Valida a disponibilidade do material ANTES do I/O de bytes."""
    metadata = revision.source_metadata or {}
    if not item.resource_id or metadata.get("resourceId") != str(
        item.resource_id
    ):
        raise _conflict()
    frozen_sha = metadata.get("sha256")
    if not isinstance(frozen_sha, str) or not frozen_sha:
        raise _conflict()
    content_type = metadata.get("contentType")
    extension = MATERIAL_MEDIA_TYPES.get(str(content_type))
    if extension is None:
        raise _conflict()
    resource = await db.get(Resource, item.resource_id)
    if resource is None or not resource.storage_key:
        raise _conflict()
    if not resource.content_sha256 or resource.content_sha256 != frozen_sha:
        # O arquivo mudou ou não é o conteúdo verificado: nunca serve bytes
        # por baixo do hash fixado na revisão.
        raise _conflict()
    license = await get_current_license(db, resource.id)
    try:
        assert_can_deliver_to_family(resource, license)
    except ResourceLicensePolicyError as exc:
        raise _conflict() from exc
    return PreparedItemFile(
        kind="material",
        item_id=item.id,
        revision_version=revision.version,
        media_type=str(content_type),
        filename=f"material-{item.id}.{extension}",
        frozen_sha256=frozen_sha,
        resource_id=resource.id,
        resource=resource,
    )


async def _read_material_bytes(
    prepared: PreparedItemFile,
) -> tuple[bytes, str]:
    """Baixa com teto real e compara o SHA-256 dos bytes ao congelado."""
    resource = prepared.resource
    if resource is None or not resource.storage_key:
        raise _conflict()
    try:
        body, _ = await storage_service.download_limited(
            resource.storage_key, max_bytes=MATERIAL_MAX_BYTES
        )
    except StorageLimitExceededError as exc:
        raise _service_unavailable(
            MATERIAL_FILE_UNAVAILABLE_DETAIL
        ) from exc
    except Exception as exc:  # noqa: BLE001 — ausente vira 409; resto vira 503
        logger.warning("F14: falha ao carregar material do portal da família")
        if is_missing_object_error(exc):
            raise _conflict() from exc
        raise _service_unavailable(MATERIAL_FILE_UNAVAILABLE_DETAIL) from exc
    if hashlib.sha256(body).hexdigest() != prepared.frozen_sha256:
        raise _conflict()
    return body, prepared.media_type


async def _prepare_report(
    db: AsyncSession,
    portal: FamilyPortal,
    item: FamilyPortalItem,
    revision: FamilyPortalItemRevision,
) -> PreparedItemFile:
    """Valida a entrega F1 congelada ANTES do render."""
    metadata = revision.source_metadata or {}
    if not item.delivery_id or metadata.get("deliveryId") != str(
        item.delivery_id
    ):
        raise _conflict()
    try:
        context = await report_delivery_service.load_fixed_delivery_context(
            db, item.delivery_id
        )
    except report_delivery_service.FixedSnapshotUnavailableError as exc:
        # Revogada/expirada/legacy/incompleta: 409 neutro, jamais o texto atual.
        raise _conflict() from exc
    if (
        context.delivery.patient_id != portal.patient_id
        or context.delivery.professional_id != portal.owner_professional_id
        or context.report.type != family_portal_content.FAMILY_REPORT_TYPE
    ):
        raise _conflict()
    try:
        document = report_delivery_service.resolve_fixed_document(context)
    except report_delivery_service.FixedSnapshotUnavailableError as exc:
        raise _conflict() from exc
    if document.report_type != family_portal_content.FAMILY_REPORT_TYPE:
        raise _conflict()
    if metadata.get("contentHash") != document.content_hash:
        raise _conflict()
    return PreparedItemFile(
        kind="report",
        item_id=item.id,
        revision_version=revision.version,
        media_type=REPORT_MEDIA_TYPE,
        filename=f"relatorio-familiar-{item.id}.pdf",
        delivery_id=context.delivery.id,
        delivery_context=context,
        document=document,
    )


def _render_report_pdf(
    prepared: PreparedItemFile, identity
) -> tuple[bytes, str]:
    """Render síncrono (roda em thread): snapshot F1 + identidade textual F2."""
    document = prepared.document
    if document is None:
        raise _conflict()
    data, media_type, _suffix = export_report(
        "pdf",
        document.report_type,
        document.patient_name,
        document.report_date,
        document.content,
        identity=identity,
    )
    return data, media_type


async def _render_report_bytes(
    prepared: PreparedItemFile,
) -> tuple[bytes, str]:
    context = prepared.delivery_context
    document = prepared.document
    if context is None or document is None:
        raise _conflict()
    try:
        identity = report_delivery_service.apply_snapshot_identity(
            await build_document_identity(context.professional), document
        )
        return await run_in_threadpool(_render_report_pdf, prepared, identity)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — render indisponível vira 503
        logger.exception("F14: falha ao renderizar relatório do portal")
        raise _service_unavailable(REPORT_FILE_UNAVAILABLE_DETAIL) from exc


async def _revalidate_after_io(
    db: AsyncSession,
    *,
    raw_token: str | None,
    prepared: PreparedItemFile,
) -> None:
    """Relê grant/audiência/revisão/licença/entrega DEPOIS do I/O (§3.5).

    Nenhum lock fica preso durante download/render; a releitura usa
    ``populate_existing``/``expire_all`` para não devolver cópias do identity
    map. Mudou -> descarta os bytes (levanta antes de montar a resposta).
    """
    db.expire_all()
    # Grant/portal/destinatário/dono/paciente — 410 quando o link morreu.
    context = await family_portal_access.resolve_public_context(db, raw_token)
    item, revision = await family_portal_content.get_published_item_rows(
        db,
        portal=context.portal,
        recipient=context.recipient,
        item_id=prepared.item_id,
        refresh=True,
    )
    if item.kind != prepared.kind or revision.version != prepared.revision_version:
        raise _conflict()
    if prepared.kind == "material":
        if prepared.resource_id is None or prepared.frozen_sha256 is None:
            raise _conflict()
        resource = await db.get(Resource, prepared.resource_id)
        if resource is None or not resource.content_sha256:
            raise _conflict()
        if resource.content_sha256 != prepared.frozen_sha256:
            raise _conflict()
        license = await get_current_license(db, resource.id)
        try:
            assert_can_deliver_to_family(resource, license)
        except ResourceLicensePolicyError as exc:
            raise _conflict() from exc
        return
    if prepared.delivery_id is None or prepared.document is None:
        raise _conflict()
    try:
        delivery_context = (
            await report_delivery_service.load_fixed_delivery_context(
                db, prepared.delivery_id
            )
        )
    except report_delivery_service.FixedSnapshotUnavailableError as exc:
        raise _conflict() from exc
    if (
        delivery_context.delivery.patient_id != context.portal.patient_id
        or delivery_context.delivery.professional_id
        != context.portal.owner_professional_id
    ):
        raise _conflict()
    try:
        document = report_delivery_service.resolve_fixed_document(
            delivery_context
        )
    except report_delivery_service.FixedSnapshotUnavailableError as exc:
        raise _conflict() from exc
    if document.content_hash != prepared.document.content_hash:
        raise _conflict()


async def load_public_item_file(
    db: AsyncSession,
    *,
    raw_token: str | None,
    item_id: UUID,
) -> tuple[bytes, str, str]:
    """Bytes autorizados de um item ``material``/``report``; (bytes, MIME, nome).

    Kind sem arquivo -> 404; item fora do público do destinatário -> 404;
    disponibilidade mudou após a autorização -> 409 neutro; storage/render
    indisponíveis -> 503. O prazo efetivo é o menor entre entrega F1 e grant.
    """
    context = await family_portal_access.resolve_public_context(db, raw_token)
    item, revision = await family_portal_content.get_published_item_rows(
        db,
        portal=context.portal,
        recipient=context.recipient,
        item_id=item_id,
    )
    if item.kind == "material":
        prepared = await _prepare_material(db, context.portal, item, revision)
        body, media_type = await _read_material_bytes(prepared)
    elif item.kind == "report":
        prepared = await _prepare_report(db, context.portal, item, revision)
        body, media_type = await _render_report_bytes(prepared)
    else:
        raise _not_found(family_portal_content.PUBLIC_ITEM_NOT_FOUND_DETAIL)
    await _revalidate_after_io(db, raw_token=raw_token, prepared=prepared)
    return body, media_type, prepared.filename
