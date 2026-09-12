"""F14 onda 2/3 — camada editorial privada e projeções de conteúdo.

Responsabilidades (Tarefas 2.1/3.1, §3.3–3.4 do plano):

- itens editoriais com ``kind``/fonte fechados: ``session_summary``/``goal``/
  ``notice`` desde a onda 2; ``material`` (F17) e ``report`` (entrega pais F1)
  habilitados integralmente na onda 3;
- rascunho → revisão publicada imutável → público vigente (pares únicos),
  com publish/withdraw/remoção de destinatário atômicos sob lock do portal;
- fingerprints privados de sessão/meta/evolução/material/relatório
  (determinísticos, sem texto clínico bruto) que detectam mudança da fonte
  sem republicar sozinhos;
- material: publicação aplica ``assert_can_deliver_to_family`` e verifica o
  arquivo no storage; a revisão congela licença/hash/MIME/tamanho/attribution
  e a entrega revalida tudo de novo (a rota pública de arquivo mora em
  ``family_portal_files``);
- relatório: a fonte é uma entrega F1 ``standard``/``pais`` com snapshot
  completo — resolvida pelo helper ESTRITO de ``report_delivery_service``
  (sem token e sem fallback para o relatório atual);
- avisos vencem por LEITURA (``expires_at`` da revisão, sem cron);
- projeção pública allowlist compartilhada com a prévia privada do
  profissional (mesmo envelope/detalhe do GET público);
- helper ``strip_recipient_from_items`` consumido pelas retiradas da onda 1
  (``withdraw_recipient``/``withdraw_recipient_for_caregiver``): remove o
  destinatário dos públicos vigentes e rascunhos SEM apagar revisões.

O token da família nunca chega aqui; nada neste módulo depende de Redis.
Mutações próprias fazem commit explícito no router ANTES do 2xx; helpers
chamados dentro de transações legadas apenas fazem flush.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import String, and_, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.ai import AIReport
from app.models.appointment import Appointment
from app.models.evolution import Evolution
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalRecipient,
)
from app.models.family_portal_content import (
    FamilyPortalItem,
    FamilyPortalItemAudience,
    FamilyPortalItemRevision,
)
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_delivery import (
    RECIPIENT_KIND_STANDARD,
    ReportDelivery,
)
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.models.session import Session
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal_content import (
    AVAILABLE_ITEM_KINDS,
    CONTENT_MODELS,
    FamilyPortalItemResponse,
    FamilyPortalItemRevisionResponse,
    FamilyPortalItemSummary,
    FamilyPortalItemUpdateRequest,
    FamilyPortalSourceCandidate,
    GoalSource,
    MaterialSource,
    PublicItemDetail,
    PublicItemSummary,
    ReportSource,
    SessionSummarySource,
)
from app.schemas.family_portal_content import (
    FamilyPortalItemCreateRequest as ItemCreateRequest,
)
from app.schemas.family_portal_content import (
    FamilyPortalItemPublishRequest as ItemPublishRequest,
)
from app.services import report_delivery_service
from app.services.family_portal_access import (
    _PATIENT_INACTIVE_MESSAGE,
    _PORTAL_DISABLED_MESSAGE,
    _PORTAL_FOREIGN_OWNER_MESSAGE,
    _PORTAL_MISSING_MESSAGE,
    _RECIPIENT_NOT_FOUND,
    _STALE_VERSION_MESSAGE,
    _as_utc,
    _conflict,
    _current_authorization_event,
    _get_portal,
    _lock_actor,
    _lock_patient,
    _not_found,
    _record_event,
    _require_recipient,
    _unprocessable,
    require_owned_patient,
)
from app.services.report_export import REPORT_TYPE_LABELS
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    ResourceLicenseStorageUnavailableError,
    assert_can_deliver_to_family,
    current_licenses_by_resource,
    evaluate_family_delivery,
    get_current_license,
    license_is_valid_for_professionals,
    verify_resource_content,
)

MAX_ITEMS_PER_PORTAL = 200
MAX_PUBLICATION_RECIPIENTS = 10

EVENT_ITEM_PUBLISHED = "item_published"
EVENT_ITEM_WITHDRAWN = "item_withdrawn"
EVENT_ITEM_RECIPIENT_REMOVED = "item_recipient_removed"

PUBLIC_ITEM_NOT_FOUND_DETAIL = "Conteúdo não encontrado."

_ITEM_NOT_FOUND = "Conteúdo não encontrado"
_SESSION_NOT_FOUND = "Sessão não encontrada"
_EVOLUTION_NOT_FOUND = "Evolução não encontrada"
_GOAL_NOT_FOUND = "Meta não encontrada"
_SESSION_NOT_DONE = (
    "A sessão escolhida ainda não foi realizada; use uma sessão passada."
)
_SESSION_APPOINTMENT_OPEN = "A consulta desta sessão ainda não foi concluída."
_SOURCE_REQUIRED = "Informe a fonte deste tipo de conteúdo."
_SOURCE_NOT_ALLOWED = "Este tipo de conteúdo não possui fonte."
_INVALID_SOURCE = "Fonte inválida para este tipo de conteúdo."
_INVALID_CONTENT = "Conteúdo inválido para este tipo de conteúdo."
_KIND_UNAVAILABLE = (
    "Este tipo de conteúdo ainda não está disponível nesta versão do portal."
)
_RECIPIENTS_EMPTY = "Escolha ao menos um responsável antes de publicar."
_RECIPIENTS_LIMIT = (
    "Limite de 10 responsáveis por publicação. Retire algum destinatário."
)
_RECIPIENTS_DUPLICATED = "Há responsáveis repetidos na lista de destinatários."
_RECIPIENTS_INACTIVE = (
    "Inclua apenas responsáveis ativos e autorizados deste portal."
)
_ITEM_CAP = (
    "Limite de 200 itens por portal. Retire itens antigos para liberar espaço."
)
_FINGERPRINT_REQUIRED = "Confirme a versão atual da fonte antes de publicar."
_SOURCE_CHANGED = (
    "A fonte clínica mudou desde a última revisão. Confira a fonte e tente "
    "novamente."
)
_NOTICE_HAS_NO_SOURCE = "Este tipo de conteúdo não possui fonte para confirmar."
_PREVIEW_RECIPIENT_INACTIVE = (
    "Este responsável está retirado do portal. Reative-o antes de "
    "pré-visualizar o conteúdo."
)
_INVALID_STORED_RECIPIENTS = "A lista de destinatários do item está inválida."

# Onda 3 — material F17 e relatório pais F1.
FAMILY_REPORT_TYPE = "pais"
_RESOURCE_NOT_FOUND = "Material não encontrado"
_DELIVERY_NOT_FOUND = "Entrega não encontrada"
_REPORT_TYPE_REQUIRED = (
    "Somente relatórios do tipo \"Relatório para Pais\" podem ser "
    "disponibilizados no portal da família."
)
_DELIVERY_SCHOOL_FORBIDDEN = (
    "Entregas escolares não podem ser disponibilizadas no portal da família."
)
_MATERIAL_HASH_MISMATCH = (
    "O arquivo do material não corresponde ao conteúdo verificado."
)
_STORAGE_UNAVAILABLE = "Armazenamento indisponível no momento. Tente novamente."
# Motivo genérico de indisponibilidade pública (§3.4): nunca expõe licença,
# entrega, titular ou estado do provedor. Mesmo texto do 409 de arquivo.
_CONTENT_UNAVAILABLE = "Este conteúdo não está disponível no momento."


def _service_unavailable(detail: str = _STORAGE_UNAVAILABLE) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
    )


# --------------------------------------------------------------------------- #
# Fingerprints privados (determinísticos, sem texto clínico bruto)
# --------------------------------------------------------------------------- #


def _dt_token(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def session_source_fingerprint(
    session: Session, evolution: Evolution | None = None
) -> str:
    """Fonte de resumo = sessão (+ evolução opcional), nunca o texto clínico."""
    return _fingerprint(
        {
            "v": 1,
            "kind": "session_summary",
            "sessionId": str(session.id),
            "sessionDate": _dt_token(session.date),
            "sessionUpdatedAt": _dt_token(session.updated_at),
            "evolutionId": str(evolution.id) if evolution is not None else None,
            "evolutionUpdatedAt": (
                _dt_token(evolution.updated_at)
                if evolution is not None
                else None
            ),
        }
    )


def goal_source_fingerprint(goal: Goal) -> str:
    return _fingerprint(
        {
            "v": 1,
            "kind": "goal",
            "goalId": str(goal.id),
            "goalUpdatedAt": _dt_token(goal.updated_at),
        }
    )


def material_source_fingerprint(
    resource: Resource, license: ResourceLicense | None
) -> str:
    """Fonte de material = arquivo verificado + licença vigente (nunca bytes).

    Metadados editoriais do ``Resource`` (título/descrição) não entram: não são
    o que a família vê. ``updated_at`` fica de fora de propósito — downloads e
    edições de catálogo não podem marcar ``sourceChanged`` sozinhos.
    """
    return _fingerprint(
        {
            "v": 1,
            "kind": "material",
            "resourceId": str(resource.id),
            "contentSha256": resource.content_sha256,
            "publicationStatus": resource.publication_status,
            "licenseId": str(license.id) if license is not None else None,
            "licenseVersion": license.version if license is not None else None,
            "licenseStatus": license.status if license is not None else None,
            "allowFamilyDelivery": (
                bool(license.allow_family_delivery)
                if license is not None
                else False
            ),
        }
    )


def report_source_fingerprint(delivery: ReportDelivery) -> str:
    """Fonte de relatório = entrega F1 congelada (versão/hash/estado)."""
    return _fingerprint(
        {
            "v": 1,
            "kind": "report",
            "deliveryId": str(delivery.id),
            "reportId": str(delivery.report_id),
            "reportVersion": report_delivery_service.delivery_report_version(
                delivery
            ),
            "contentHash": report_delivery_service.delivery_content_hash(
                delivery
            ),
            "revokedAt": _dt_token(delivery.revoked_at),
            "expiresAt": _dt_token(delivery.expires_at),
        }
    )


def _clinic_date(value: datetime) -> date:
    timezone = get_settings().clinic_timezone
    return _as_utc(value).astimezone(ZoneInfo(timezone)).date()


# --------------------------------------------------------------------------- #
# Validação de conteúdo/fonte (servidor como fonte da verdade)
# --------------------------------------------------------------------------- #


def _validate_content(kind: str, raw: dict) -> dict:
    model = CONTENT_MODELS.get(kind)
    if model is None:
        raise _unprocessable(_KIND_UNAVAILABLE)
    try:
        parsed = model.model_validate(raw)
    except ValidationError as exc:
        raise _unprocessable(_INVALID_CONTENT) from exc
    return parsed.model_dump(by_alias=True)


@dataclass(frozen=True)
class ResolvedItemSource:
    """Fonte validada (posse/estado) + fingerprint + metadados de revisão."""

    payload: dict | None
    fingerprint: str | None
    metadata: dict
    session: Session | None = None
    evolution: Evolution | None = None
    goal: Goal | None = None
    resource: Resource | None = None
    license: ResourceLicense | None = None
    delivery: ReportDelivery | None = None


async def _resource_visible_to_actor(
    db: AsyncSession, resource: Resource, actor: Professional
) -> bool:
    """Próprio OU global publicado com licença vigente para profissionais."""
    if resource.owner_professional_id == actor.id:
        return True
    if resource.owner_professional_id is not None:
        return False
    license = await get_current_license(db, resource.id)
    return license_is_valid_for_professionals(license, resource)


def _material_source_metadata(
    resource: Resource, license: ResourceLicense | None
) -> dict:
    """Metadados tipados da revisão de material (produzidos pelo servidor)."""
    return {
        "resourceId": str(resource.id),
        "licenseId": str(license.id) if license is not None else None,
        "licenseVersion": license.version if license is not None else None,
        "sha256": resource.content_sha256,
        "contentType": resource.content_type,
        "sizeBytes": resource.file_size_bytes,
        "attribution": (license.attribution or "") if license is not None else "",
    }


async def _resolve_item_source(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    kind: str,
    source: dict | None,
    for_publish: bool = False,
    verify_storage: bool = False,
) -> ResolvedItemSource:
    """Valida a fonte do item para o dono/caso; nunca aceita fonte alheia.

    ``for_publish`` converte fonte inelegível em 409 (estado mudou desde o
    rascunho); no rascunho a mesma condição é 422 (a fonte nunca serviria).
    ``verify_storage`` (publicação de material) baixa o arquivo real com teto
    de bytes e confere o SHA-256 — falha de storage vira 503.
    """
    if kind == "notice":
        if source:
            raise _unprocessable(_SOURCE_NOT_ALLOWED)
        return ResolvedItemSource(payload=None, fingerprint=None, metadata={})
    if kind not in AVAILABLE_ITEM_KINDS:
        raise _unprocessable(_KIND_UNAVAILABLE)
    if not source:
        raise _unprocessable(_SOURCE_REQUIRED)
    if kind == "material":
        try:
            parsed_resource = MaterialSource.model_validate(source)
        except ValidationError as exc:
            raise _unprocessable(_INVALID_SOURCE) from exc
        resource = await db.get(Resource, parsed_resource.resource_id)
        if resource is None or not await _resource_visible_to_actor(
            db, resource, actor
        ):
            raise _not_found(_RESOURCE_NOT_FOUND)
        license = await get_current_license(db, resource.id)
        try:
            assert_can_deliver_to_family(resource, license)
        except ResourceLicensePolicyError as exc:
            raise (
                _conflict(exc.detail) if for_publish else _unprocessable(exc.detail)
            ) from exc
        metadata = _material_source_metadata(resource, license)
        if verify_storage:
            try:
                digest = await verify_resource_content(resource)
            except ResourceLicensePolicyError as exc:
                # Arquivo ausente/ilegível no storage: o estado mudou depois do
                # rascunho — nunca publica um hash não conferido.
                raise _conflict(exc.detail) from exc
            except ResourceLicenseStorageUnavailableError as exc:
                raise _service_unavailable(exc.detail) from exc
            if resource.content_sha256 and digest != resource.content_sha256:
                raise _conflict(_MATERIAL_HASH_MISMATCH)
            metadata["sha256"] = digest
            metadata["sizeBytes"] = resource.file_size_bytes
        return ResolvedItemSource(
            payload={"resourceId": str(resource.id)},
            fingerprint=material_source_fingerprint(resource, license),
            metadata=metadata,
            resource=resource,
            license=license,
        )
    if kind == "report":
        try:
            parsed_delivery = ReportSource.model_validate(source)
        except ValidationError as exc:
            raise _unprocessable(_INVALID_SOURCE) from exc
        delivery = await db.get(ReportDelivery, parsed_delivery.delivery_id)
        if (
            delivery is None
            or delivery.patient_id != patient.id
            or delivery.professional_id != actor.id
        ):
            raise _not_found(_DELIVERY_NOT_FOUND)
        report = await db.get(AIReport, delivery.report_id)
        if (
            report is None
            or report.patient_id != patient.id
            or report.professional_id != actor.id
        ):
            raise _not_found(_DELIVERY_NOT_FOUND)
        if delivery.recipient_kind != RECIPIENT_KIND_STANDARD:
            raise (
                _conflict(_DELIVERY_SCHOOL_FORBIDDEN)
                if for_publish
                else _unprocessable(_DELIVERY_SCHOOL_FORBIDDEN)
            )
        if report.type != FAMILY_REPORT_TYPE:
            raise (
                _conflict(_REPORT_TYPE_REQUIRED)
                if for_publish
                else _unprocessable(_REPORT_TYPE_REQUIRED)
            )
        issue = report_delivery_service.family_delivery_issue(delivery)
        if issue is not None:
            # Legacy/incompleta/revogada/expirada: recusado ANTES de qualquer
            # resolução legada — jamais cai no texto atual do relatório.
            detail = report_delivery_service.family_delivery_issue_message(issue)
            raise _conflict(detail) if for_publish else _unprocessable(detail)
        return ResolvedItemSource(
            payload={"deliveryId": str(delivery.id)},
            fingerprint=report_source_fingerprint(delivery),
            metadata={
                "deliveryId": str(delivery.id),
                "reportId": str(report.id),
                "reportVersion": report_delivery_service.delivery_report_version(
                    delivery
                ),
                "contentHash": report_delivery_service.delivery_content_hash(
                    delivery
                ),
                "reportType": report.type,
            },
            delivery=delivery,
        )
    if kind == "session_summary":
        try:
            parsed = SessionSummarySource.model_validate(source)
        except ValidationError as exc:
            raise _unprocessable(_INVALID_SOURCE) from exc
        session = await db.get(Session, parsed.session_id)
        if (
            session is None
            or session.patient_id != patient.id
            or session.professional_id != actor.id
        ):
            raise _not_found(_SESSION_NOT_FOUND)
        if _as_utc(session.date) > utcnow():
            raise _unprocessable(_SESSION_NOT_DONE)
        if session.appointment_id is not None:
            appointment = await db.get(Appointment, session.appointment_id)
            if (
                appointment is None
                or appointment.status != "concluido"
                or appointment.patient_id != patient.id
                or appointment.professional_id != actor.id
            ):
                raise _unprocessable(_SESSION_APPOINTMENT_OPEN)
        evolution = None
        if parsed.evolution_id is not None:
            evolution = await db.get(Evolution, parsed.evolution_id)
            if (
                evolution is None
                or evolution.session_id != session.id
                or evolution.patient_id != patient.id
                or evolution.professional_id != actor.id
            ):
                raise _not_found(_EVOLUTION_NOT_FOUND)
        return ResolvedItemSource(
            payload={
                "sessionId": str(session.id),
                "evolutionId": str(evolution.id) if evolution is not None else None,
            },
            fingerprint=session_source_fingerprint(session, evolution),
            metadata={
                "sessionId": str(session.id),
                "sessionDate": _clinic_date(session.date).isoformat(),
                "evolutionId": (
                    str(evolution.id) if evolution is not None else None
                ),
            },
            session=session,
            evolution=evolution,
        )
    # goal
    try:
        parsed_goal = GoalSource.model_validate(source)
    except ValidationError as exc:
        raise _unprocessable(_INVALID_SOURCE) from exc
    goal = await db.get(Goal, parsed_goal.goal_id)
    if (
        goal is None
        or goal.patient_id != patient.id
        or goal.professional_id != actor.id
    ):
        raise _not_found(_GOAL_NOT_FOUND)
    return ResolvedItemSource(
        payload={"goalId": str(goal.id)},
        fingerprint=goal_source_fingerprint(goal),
        metadata={"goalId": str(goal.id)},
        goal=goal,
    )


async def _current_source_fingerprint(
    db: AsyncSession, item: FamilyPortalItem
) -> str | None:
    """Fingerprint da fonte VIGENTE; None quando a fonte sumiu do caso."""
    if item.kind == "session_summary":
        session = (
            await db.get(Session, item.session_id) if item.session_id else None
        )
        if session is None:
            return None
        evolution = (
            await db.get(Evolution, item.evolution_id)
            if item.evolution_id
            else None
        )
        return session_source_fingerprint(session, evolution)
    if item.kind == "goal":
        goal = await db.get(Goal, item.goal_id) if item.goal_id else None
        if goal is None:
            return None
        return goal_source_fingerprint(goal)
    if item.kind == "material":
        resource = (
            await db.get(Resource, item.resource_id) if item.resource_id else None
        )
        if resource is None:
            return None
        license = await get_current_license(db, resource.id)
        return material_source_fingerprint(resource, license)
    if item.kind == "report":
        delivery = (
            await db.get(ReportDelivery, item.delivery_id)
            if item.delivery_id
            else None
        )
        if delivery is None:
            return None
        return report_source_fingerprint(delivery)
    return None


def _item_source_payload(item: FamilyPortalItem) -> dict | None:
    if item.kind == "session_summary":
        return {
            "sessionId": str(item.session_id),
            "evolutionId": (
                str(item.evolution_id) if item.evolution_id else None
            ),
        }
    if item.kind == "goal":
        return {"goalId": str(item.goal_id)}
    if item.kind == "material":
        return {"resourceId": str(item.resource_id)}
    if item.kind == "report":
        return {"deliveryId": str(item.delivery_id)}
    return None


# --------------------------------------------------------------------------- #
# Consulta de itens e respostas privadas
# --------------------------------------------------------------------------- #


async def _get_item(
    db: AsyncSession,
    portal: FamilyPortal,
    item_id: UUID,
    *,
    lock: bool = False,
) -> FamilyPortalItem | None:
    query = select(FamilyPortalItem).where(
        FamilyPortalItem.id == item_id,
        FamilyPortalItem.portal_id == portal.id,
    )
    if lock:
        query = query.with_for_update().execution_options(
            populate_existing=True
        )
    return await db.scalar(query)


async def _require_item(
    db: AsyncSession, portal: FamilyPortal, item_id: UUID, *, lock: bool = False
) -> FamilyPortalItem:
    item = await _get_item(db, portal, item_id, lock=lock)
    if item is None:
        raise _not_found(_ITEM_NOT_FOUND)
    return item


async def _require_content_portal(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    require_enabled: bool = True,
) -> FamilyPortal:
    """Portal do dono sob lock; ações protetivas dispensam ``enabled``."""
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None:
        raise _conflict(_PORTAL_MISSING_MESSAGE)
    if portal.owner_professional_id != actor.id:
        raise _conflict(_PORTAL_FOREIGN_OWNER_MESSAGE)
    if require_enabled and not portal.enabled:
        raise _conflict(_PORTAL_DISABLED_MESSAGE)
    return portal


def _has_unpublished_changes(item: FamilyPortalItem) -> bool:
    return (
        item.published_version is None
        or item.version > item.published_version
    )


async def _item_response(
    db: AsyncSession, item: FamilyPortalItem
) -> FamilyPortalItemResponse:
    fingerprint: str | None = None
    source_changed = False
    if item.kind != "notice":
        fingerprint = await _current_source_fingerprint(db, item)
        source_changed = (
            fingerprint is None
            or item.draft_source_fingerprint != fingerprint
        )
    return FamilyPortalItemResponse(
        id=str(item.id),
        kind=item.kind,
        source=_item_source_payload(item),
        draft_content=dict(item.draft_content or {}),
        draft_recipient_ids=[str(value) for value in (item.draft_recipient_ids or [])],
        status=item.status,
        version=item.version,
        published_version=item.published_version,
        has_unpublished_changes=_has_unpublished_changes(item),
        source_fingerprint=fingerprint,
        source_changed=source_changed,
        published_at=_as_utc(item.published_at) if item.published_at else None,
        updated_at=_as_utc(item.updated_at),
    )


async def _item_summary(
    db: AsyncSession, item: FamilyPortalItem
) -> FamilyPortalItemSummary:
    source_changed = False
    if item.kind != "notice":
        fingerprint = await _current_source_fingerprint(db, item)
        source_changed = (
            fingerprint is None or item.draft_source_fingerprint != fingerprint
        )
    return FamilyPortalItemSummary(
        id=str(item.id),
        kind=item.kind,
        title=str((item.draft_content or {}).get("title") or ""),
        status=item.status,
        version=item.version,
        published_version=item.published_version,
        has_unpublished_changes=_has_unpublished_changes(item),
        source_changed=source_changed,
        recipient_ids=[str(value) for value in (item.draft_recipient_ids or [])],
        published_at=_as_utc(item.published_at) if item.published_at else None,
    )


def _parse_stored_recipients(item: FamilyPortalItem) -> list[UUID]:
    parsed: list[UUID] = []
    for raw in item.draft_recipient_ids or []:
        try:
            parsed.append(raw if isinstance(raw, UUID) else UUID(str(raw)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise _unprocessable(_INVALID_STORED_RECIPIENTS) from exc
    return parsed


async def _validate_draft_recipients(
    db: AsyncSession,
    portal: FamilyPortal,
    recipient_ids: list[UUID],
) -> list[UUID]:
    """Rascunho aceita lista vazia; IDs precisam existir e estar ativos."""
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for value in recipient_ids:
        if value in seen:
            raise _unprocessable(_RECIPIENTS_DUPLICATED)
        seen.add(value)
        ordered.append(value)
    if len(ordered) > MAX_PUBLICATION_RECIPIENTS:
        raise _unprocessable(_RECIPIENTS_LIMIT)
    if not ordered:
        return []
    rows = (
        (
            await db.execute(
                select(FamilyPortalRecipient).where(
                    FamilyPortalRecipient.portal_id == portal.id,
                    FamilyPortalRecipient.id.in_(ordered),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    for value in ordered:
        recipient = by_id.get(value)
        if recipient is None:
            raise _not_found(_RECIPIENT_NOT_FOUND)
        if not recipient.active:
            raise _conflict(_RECIPIENTS_INACTIVE)
    return ordered


async def _lock_publish_recipients(
    db: AsyncSession,
    portal: FamilyPortal,
    recipient_ids: list[UUID],
) -> list[UUID]:
    """Trava os destinatários ANTES do item (§3.6), ordenados por UUID."""
    if not recipient_ids:
        return []
    orders = sorted(set(recipient_ids), key=str)
    await db.execute(
        select(FamilyPortalRecipient.id)
        .where(
            FamilyPortalRecipient.portal_id == portal.id,
            FamilyPortalRecipient.id.in_(orders),
        )
        .order_by(FamilyPortalRecipient.id)
        .with_for_update()
    )
    return orders


async def _validate_publish_recipients(
    db: AsyncSession,
    portal: FamilyPortal,
    stored_ids: list[str],
) -> list[UUID]:
    parsed: list[UUID] = []
    for raw in stored_ids or []:
        try:
            parsed.append(raw if isinstance(raw, UUID) else UUID(str(raw)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise _unprocessable(_INVALID_STORED_RECIPIENTS) from exc
    if not parsed:
        raise _unprocessable(_RECIPIENTS_EMPTY)
    if len(parsed) != len(set(parsed)):
        raise _unprocessable(_RECIPIENTS_DUPLICATED)
    if len(parsed) > MAX_PUBLICATION_RECIPIENTS:
        raise _unprocessable(_RECIPIENTS_LIMIT)
    rows = (
        (
            await db.execute(
                select(FamilyPortalRecipient).where(
                    FamilyPortalRecipient.portal_id == portal.id,
                    FamilyPortalRecipient.id.in_(parsed),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    for value in parsed:
        recipient = by_id.get(value)
        if recipient is None:
            raise _not_found(_RECIPIENT_NOT_FOUND)
        if not recipient.active:
            raise _conflict(_RECIPIENTS_INACTIVE)
    return parsed


# --------------------------------------------------------------------------- #
# §3.3 — fontes privadas (candidatos do picker)
# --------------------------------------------------------------------------- #


def _like_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    )
    return f"%{escaped}%"


async def _list_session_sources(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    q: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalSourceCandidate]:
    now = utcnow()
    conditions = [
        Session.patient_id == patient.id,
        Session.professional_id == actor.id,
        Session.date <= now,
    ]
    if q:
        conditions.append(Session.type.ilike(_like_pattern(q), escape="\\"))
    total = await db.scalar(
        select(func.count()).select_from(Session).where(*conditions)
    )
    rows = (
        (
            await db.execute(
                select(Session)
                .where(*conditions)
                .order_by(Session.date.desc(), Session.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    appointment_ids = [
        row.appointment_id for row in rows if row.appointment_id is not None
    ]
    appointments: dict[UUID, Appointment] = {}
    if appointment_ids:
        loaded = (
            (
                await db.execute(
                    select(Appointment).where(
                        Appointment.id.in_(appointment_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        appointments = {row.id: row for row in loaded}
    items: list[FamilyPortalSourceCandidate] = []
    for row in rows:
        eligible = True
        reason: str | None = None
        if row.appointment_id is not None:
            appointment = appointments.get(row.appointment_id)
            if appointment is None or appointment.status != "concluido":
                eligible = False
                reason = "Consulta ainda não concluída"
        items.append(
            FamilyPortalSourceCandidate(
                id=str(row.id),
                kind="session",
                label=(row.type or "").strip() or "Sessão",
                date=_clinic_date(row.date),
                source_fingerprint=session_source_fingerprint(row),
                eligible=eligible,
                unavailable_reason=reason,
            )
        )
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def _list_goal_sources(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    q: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalSourceCandidate]:
    conditions = [
        Goal.patient_id == patient.id,
        Goal.professional_id == actor.id,
    ]
    if q:
        conditions.append(Goal.title.ilike(_like_pattern(q), escape="\\"))
    total = await db.scalar(
        select(func.count()).select_from(Goal).where(*conditions)
    )
    rows = (
        (
            await db.execute(
                select(Goal)
                .where(*conditions)
                .order_by(Goal.start_date.desc(), Goal.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        FamilyPortalSourceCandidate(
            id=str(row.id),
            kind="goal",
            label=(row.title or "").strip() or "Meta",
            date=row.start_date,
            source_fingerprint=goal_source_fingerprint(row),
            eligible=True,
            unavailable_reason=None,
        )
        for row in rows
    ]
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def _list_resource_sources(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    q: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalSourceCandidate]:
    """Materiais visíveis ao dono com avaliação explícita do gate familiar.

    Próprios (qualquer estado editorial, com motivo quando inelegíveis) OU
    globais publicados com licença vigente para profissionais. Notas/answers
    nunca saem; a página é montada depois dos filtros (como o catálogo).
    """
    conditions = [
        or_(
            Resource.owner_professional_id == actor.id,
            and_(
                Resource.owner_professional_id.is_(None),
                Resource.publication_status == "published",
            ),
        )
    ]
    if q:
        pattern = _like_pattern(q)
        conditions.append(
            or_(
                Resource.title.ilike(pattern, escape="\\"),
                Resource.description.ilike(pattern, escape="\\"),
            )
        )
    rows = (
        (
            await db.execute(
                select(Resource)
                .where(*conditions)
                .order_by(Resource.updated_at.desc(), Resource.id.desc())
            )
        )
        .scalars()
        .all()
    )
    licenses = await current_licenses_by_resource(db, [row.id for row in rows])
    candidates: list[FamilyPortalSourceCandidate] = []
    for row in rows:
        license_row = licenses.get(row.id)
        if row.owner_professional_id is None and not license_is_valid_for_professionals(
            license_row, row
        ):
            continue
        eligible, reason = evaluate_family_delivery(row, license_row)
        candidates.append(
            FamilyPortalSourceCandidate(
                id=str(row.id),
                kind="resource",
                label=(row.title or "").strip() or "Material",
                date=None,
                source_fingerprint=material_source_fingerprint(row, license_row),
                eligible=eligible,
                unavailable_reason=reason,
            )
        )
    total = len(candidates)
    start = (page - 1) * limit
    return PaginatedResponse(
        items=candidates[start : start + limit],
        total=total,
        page=page,
        limit=limit,
    )


async def _list_report_delivery_sources(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    q: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalSourceCandidate]:
    """Entregas F1 do caso em relatórios ``pais``; demais tipos ficam de fora.

    Entregas escolares e relatórios de outro tipo não são listados; cada
    entrega do caso indica o motivo quando não pode ser usada (legacy, expirada,
    revogada ou incompleta). ``q`` filtra pela data ISO do relatório.
    """
    conditions = [
        ReportDelivery.patient_id == patient.id,
        ReportDelivery.professional_id == actor.id,
        ReportDelivery.recipient_kind == RECIPIENT_KIND_STANDARD,
        AIReport.id == ReportDelivery.report_id,
        AIReport.patient_id == patient.id,
        AIReport.professional_id == actor.id,
        AIReport.type == FAMILY_REPORT_TYPE,
    ]
    if q:
        conditions.append(
            cast(AIReport.date, String).ilike(_like_pattern(q), escape="\\")
        )
    total = await db.scalar(
        select(func.count())
        .select_from(ReportDelivery)
        .join(AIReport, AIReport.id == ReportDelivery.report_id)
        .where(*conditions)
    )
    rows = (
        await db.execute(
            select(ReportDelivery, AIReport)
            .join(AIReport, AIReport.id == ReportDelivery.report_id)
            .where(*conditions)
            .order_by(
                ReportDelivery.created_at.desc(), ReportDelivery.id.desc()
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).all()
    items: list[FamilyPortalSourceCandidate] = []
    for delivery, report in rows:
        issue = report_delivery_service.family_delivery_issue(delivery)
        items.append(
            FamilyPortalSourceCandidate(
                id=str(delivery.id),
                kind="reportDelivery",
                label=(
                    f"{REPORT_TYPE_LABELS.get(report.type, report.type)}"
                    f" · {report.date.isoformat()}"
                ),
                date=report.date,
                source_fingerprint=report_source_fingerprint(delivery),
                eligible=issue is None,
                unavailable_reason=(
                    report_delivery_service.family_delivery_issue_message(issue)
                    if issue is not None
                    else None
                ),
            )
        )
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def list_sources(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    kind: str,
    q: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalSourceCandidate]:
    """GET P/sources: candidatos do dono/caso; notas/answers nunca saem."""
    patient = await require_owned_patient(db, patient_id, actor)
    if kind == "session":
        return await _list_session_sources(
            db, patient, actor, q=q, page=page, limit=limit
        )
    if kind == "resource":
        return await _list_resource_sources(
            db, patient, actor, q=q, page=page, limit=limit
        )
    if kind == "reportDelivery":
        return await _list_report_delivery_sources(
            db, patient, actor, q=q, page=page, limit=limit
        )
    return await _list_goal_sources(
        db, patient, actor, q=q, page=page, limit=limit
    )


# --------------------------------------------------------------------------- #
# §3.3 — CRUD editorial privado
# --------------------------------------------------------------------------- #


async def create_item(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    body: ItemCreateRequest,
) -> FamilyPortalItemResponse:
    """POST P/items: rascunho version=1; destinatários podem ser []."""
    if body.kind not in AVAILABLE_ITEM_KINDS:
        raise _unprocessable(_KIND_UNAVAILABLE)
    patient = await require_owned_patient(db, patient_id, actor)
    if patient.status == "inativo":
        raise _conflict(_PATIENT_INACTIVE_MESSAGE)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_content_portal(db, patient, actor)
    count = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalItem)
        .where(
            FamilyPortalItem.portal_id == portal.id,
            FamilyPortalItem.status != "withdrawn",
        )
    )
    if int(count or 0) >= MAX_ITEMS_PER_PORTAL:
        raise _unprocessable(_ITEM_CAP)
    content = _validate_content(body.kind, body.content)
    source = await _resolve_item_source(
        db, patient, actor, kind=body.kind, source=body.source
    )
    recipients = await _validate_draft_recipients(
        db, portal, body.recipient_ids
    )
    now = utcnow()
    item = FamilyPortalItem(
        portal_id=portal.id,
        kind=body.kind,
        status="draft",
        version=1,
        published_version=None,
        draft_content=content,
        draft_recipient_ids=[str(value) for value in recipients],
        draft_source_fingerprint=source.fingerprint,
        session_id=(
            source.session.id if source.session is not None else None
        ),
        evolution_id=(
            source.evolution.id if source.evolution is not None else None
        ),
        goal_id=source.goal.id if source.goal is not None else None,
        resource_id=(
            source.resource.id if source.resource is not None else None
        ),
        delivery_id=(
            source.delivery.id if source.delivery is not None else None
        ),
        created_by_professional_id=actor.id,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    await db.flush()
    return await _item_response(db, item)


async def list_items(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    kind: str | None,
    status_filter: str | None,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalItemSummary]:
    """GET P/items: itens do portal do dono, ordenados por edição recente."""
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        return PaginatedResponse(items=[], total=0, page=page, limit=limit)
    conditions = [FamilyPortalItem.portal_id == portal.id]
    if kind:
        conditions.append(FamilyPortalItem.kind == kind)
    if status_filter:
        conditions.append(FamilyPortalItem.status == status_filter)
    total = await db.scalar(
        select(func.count()).select_from(FamilyPortalItem).where(*conditions)
    )
    rows = (
        (
            await db.execute(
                select(FamilyPortalItem)
                .where(*conditions)
                .order_by(
                    FamilyPortalItem.updated_at.desc(),
                    FamilyPortalItem.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [await _item_summary(db, row) for row in rows]
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def get_item(
    db: AsyncSession, patient_id: UUID, actor: Professional, item_id: UUID
) -> FamilyPortalItemResponse:
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _not_found(_ITEM_NOT_FOUND)
    item = await _require_item(db, portal, item_id)
    return await _item_response(db, item)


async def update_item(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    item_id: UUID,
    body: FamilyPortalItemUpdateRequest,
) -> FamilyPortalItemResponse:
    """PATCH P/items: kind/source imutáveis; só mudanças reais versionam."""
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_content_portal(db, patient, actor)
    item = await _require_item(db, portal, item_id, lock=True)
    if body.expected_version != item.version:
        raise _conflict(_STALE_VERSION_MESSAGE)
    changed = False
    if body.content is not None:
        content = _validate_content(item.kind, body.content)
        if content != dict(item.draft_content or {}):
            item.draft_content = content
            changed = True
    if body.recipient_ids is not None:
        recipients = await _validate_draft_recipients(
            db, portal, body.recipient_ids
        )
        stored = [str(value) for value in (item.draft_recipient_ids or [])]
        if [str(value) for value in recipients] != stored:
            item.draft_recipient_ids = [str(value) for value in recipients]
            changed = True
    if changed:
        # Recalcula o fingerprint privado da fonte e versiona o rascunho.
        if item.kind != "notice":
            item.draft_source_fingerprint = await _current_source_fingerprint(
                db, item
            )
        item.version += 1
        await db.flush()
    return await _item_response(db, item)


async def publish_item(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    item_id: UUID,
    body: ItemPublishRequest,
) -> FamilyPortalItemResponse:
    """POST P/items/{id}/publish: revisão imutável + público, atomicamente.

    Revalida fonte/estado, autorização de TODOS os destinatários, limites e
    fingerprint; sem mudança desde a última publicação a versão corrente é
    no-op (sem renovar prazo de aviso nem criar revisão/novidade).
    """
    patient = await require_owned_patient(db, patient_id, actor)
    if patient.status == "inativo":
        raise _conflict(_PATIENT_INACTIVE_MESSAGE)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_content_portal(db, patient, actor)
    item = await _require_item(db, portal, item_id)
    if item.kind not in AVAILABLE_ITEM_KINDS:
        raise _unprocessable(_KIND_UNAVAILABLE)
    if body.expected_version != item.version:
        raise _conflict(_STALE_VERSION_MESSAGE)
    # Trava destinatários antes do item e revalida o estado DEPOIS do lock.
    await _lock_publish_recipients(
        db, portal, _parse_stored_recipients(item)
    )
    item = await _require_item(db, portal, item_id, lock=True)
    if body.expected_version != item.version:
        raise _conflict(_STALE_VERSION_MESSAGE)
    recipients = await _validate_publish_recipients(
        db, portal, item.draft_recipient_ids or []
    )
    content = _validate_content(item.kind, dict(item.draft_content or {}))
    if item.kind == "notice":
        if body.expected_source_fingerprint is not None:
            raise _unprocessable(_NOTICE_HAS_NO_SOURCE)
        source_fingerprint = None
        source_metadata: dict = {}
    else:
        if not body.expected_source_fingerprint:
            raise _unprocessable(_FINGERPRINT_REQUIRED)
        source = await _resolve_item_source(
            db,
            patient,
            actor,
            kind=item.kind,
            source=_item_source_payload(item),
            for_publish=True,
            # Material: revalida licença corrente E o arquivo real no storage.
            verify_storage=item.kind == "material",
        )
        if source.fingerprint != body.expected_source_fingerprint:
            raise _conflict(_SOURCE_CHANGED)
        source_fingerprint = source.fingerprint
        source_metadata = source.metadata
    if item.status == "published" and item.published_version == item.version:
        # No-op: nada mudou desde a última publicação (não renova aviso).
        if item.kind != "notice":
            item.draft_source_fingerprint = source_fingerprint
            await db.flush()
        return await _item_response(db, item)
    now = utcnow()
    expires_at = (
        now + timedelta(days=int(content["expiresInDays"]))
        if item.kind == "notice"
        else None
    )
    revision = FamilyPortalItemRevision(
        item_id=item.id,
        version=item.version,
        content=content,
        recipient_ids=[str(value) for value in recipients],
        source_fingerprint=source_fingerprint,
        source_metadata=source_metadata,
        published_by_professional_id=actor.id,
        published_at=now,
        expires_at=expires_at,
    )
    db.add(revision)
    try:
        await db.flush()
    except IntegrityError as exc:  # UNIQUE(item_id, version): última defesa
        await db.rollback()
        raise _conflict(_STALE_VERSION_MESSAGE) from exc
    await db.execute(
        delete(FamilyPortalItemAudience).where(
            FamilyPortalItemAudience.item_id == item.id
        )
    )
    for recipient_id in recipients:
        db.add(
            FamilyPortalItemAudience(
                item_id=item.id, recipient_id=recipient_id
            )
        )
    item.status = "published"
    item.published_version = item.version
    item.published_at = now
    item.withdrawn_at = None
    if item.kind != "notice":
        item.draft_source_fingerprint = source_fingerprint
    _record_event(
        db,
        portal=portal,
        actor=actor,
        event_type=EVENT_ITEM_PUBLISHED,
        item_id=item.id,
        payload={
            "kind": item.kind,
            "version": item.version,
            "recipients": len(recipients),
        },
    )
    await db.flush()
    return await _item_response(db, item)


async def withdraw_item(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    item_id: UUID,
) -> FamilyPortalItemResponse:
    """POST P/items/{id}/withdraw: retirada imediata, idempotente e protetiva.

    Versiona a transição para invalidar publicação concorrente preparada antes
    da retirada; preserva texto/revisões e esvazia o público vigente.
    """
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_content_portal(
        db, patient, actor, require_enabled=False
    )
    item = await _require_item(db, portal, item_id, lock=True)
    if item.status != "withdrawn":
        item.status = "withdrawn"
        item.version += 1
        item.withdrawn_at = utcnow()
        await db.execute(
            delete(FamilyPortalItemAudience).where(
                FamilyPortalItemAudience.item_id == item.id
            )
        )
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_ITEM_WITHDRAWN,
            item_id=item.id,
            payload={"kind": item.kind, "version": item.version},
        )
        await db.flush()
    return await _item_response(db, item)


async def remove_item_recipient(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    item_id: UUID,
    recipient_id: UUID,
) -> None:
    """DELETE P/items/{id}/recipients/{rid}: retirada protetiva individual.

    Remove do público vigente e do rascunho, audita e versiona quando mudou;
    reintroduzir exige nova revisão/publicação explícita. Idempotente.
    """
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_content_portal(
        db, patient, actor, require_enabled=False
    )
    recipient = await _require_recipient(
        db, portal, recipient_id, lock=True
    )
    item = await _require_item(db, portal, item_id, lock=True)
    changed = False
    audience_row = await db.scalar(
        select(FamilyPortalItemAudience).where(
            FamilyPortalItemAudience.item_id == item.id,
            FamilyPortalItemAudience.recipient_id == recipient.id,
        )
    )
    if audience_row is not None:
        await db.delete(audience_row)
        changed = True
    stored = [str(value) for value in (item.draft_recipient_ids or [])]
    if str(recipient.id) in stored:
        item.draft_recipient_ids = [
            value for value in stored if value != str(recipient.id)
        ]
        changed = True
    if changed:
        item.version += 1
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_ITEM_RECIPIENT_REMOVED,
            item_id=item.id,
            recipient_id=recipient.id,
            payload={"scope": "item"},
        )
        await db.flush()


async def list_item_revisions(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    item_id: UUID,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalItemRevisionResponse]:
    """Histórico privado append-only; nunca servido à família."""
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _not_found(_ITEM_NOT_FOUND)
    item = await _require_item(db, portal, item_id)
    total = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalItemRevision)
        .where(FamilyPortalItemRevision.item_id == item.id)
    )
    rows = (
        (
            await db.execute(
                select(FamilyPortalItemRevision)
                .where(FamilyPortalItemRevision.item_id == item.id)
                .order_by(
                    FamilyPortalItemRevision.version.desc(),
                    FamilyPortalItemRevision.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        FamilyPortalItemRevisionResponse(
            version=row.version,
            published_at=_as_utc(row.published_at),
            published_by_professional_id=str(row.published_by_professional_id),
            content=dict(row.content or {}),
            recipient_ids=[str(value) for value in (row.recipient_ids or [])],
            source_fingerprint=row.source_fingerprint,
            source_metadata=dict(row.source_metadata or {}),
        )
        for row in rows
    ]
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


# --------------------------------------------------------------------------- #
# §3.4 — projeções públicas (compartilhadas com a prévia privada)
# --------------------------------------------------------------------------- #


def _published_items_select(*, portal: FamilyPortal, recipient, kind: str, now: datetime):
    query = (
        select(FamilyPortalItem)
        .join(
            FamilyPortalItemAudience,
            FamilyPortalItemAudience.item_id == FamilyPortalItem.id,
        )
        .join(
            FamilyPortalItemRevision,
            and_(
                FamilyPortalItemRevision.item_id == FamilyPortalItem.id,
                FamilyPortalItemRevision.version
                == FamilyPortalItem.published_version,
            ),
        )
        .where(
            FamilyPortalItem.portal_id == portal.id,
            FamilyPortalItem.status == "published",
            FamilyPortalItem.kind == kind,
            FamilyPortalItemAudience.recipient_id == recipient.id,
        )
    )
    if kind == "notice":
        # Aviso vence por leitura: expirado some da listagem (sem cron).
        query = query.where(FamilyPortalItemRevision.expires_at > now)
    return query


async def _published_material_availability(
    db: AsyncSession,
    portal: FamilyPortal,
    item: FamilyPortalItem,
    revision: FamilyPortalItemRevision,
) -> tuple[bool, str | None]:
    """Disponibilidade DB-only do material (sem I/O): licença vigente + hash.

    Nunca devolve motivo específico — quem vê a família recebe apenas o
    genérico, sem titular/licença/estado do provedor.
    """
    metadata = revision.source_metadata or {}
    if (
        not item.resource_id
        or metadata.get("resourceId") != str(item.resource_id)
    ):
        return False, _CONTENT_UNAVAILABLE
    frozen_sha = metadata.get("sha256")
    if not isinstance(frozen_sha, str) or not frozen_sha:
        return False, _CONTENT_UNAVAILABLE
    resource = await db.get(Resource, item.resource_id)
    if resource is None or resource.storage_key is None:
        return False, _CONTENT_UNAVAILABLE
    if not resource.content_sha256 or resource.content_sha256 != frozen_sha:
        return False, _CONTENT_UNAVAILABLE
    license = await get_current_license(db, resource.id)
    try:
        assert_can_deliver_to_family(resource, license)
    except ResourceLicensePolicyError:
        return False, _CONTENT_UNAVAILABLE
    return True, None


async def _published_report_availability(
    db: AsyncSession,
    portal: FamilyPortal,
    item: FamilyPortalItem,
    revision: FamilyPortalItemRevision,
) -> tuple[bool, str | None]:
    """Disponibilidade DB-only da entrega F1: snapshot estrito + vigência.

    O prazo efetivo é o menor entre a entrega F1 e o grant F14 (o grant é
    validado antes, na resolução pública); a entrega revogada/expirada ou sem
    snapshot completo deixa o card autorizado como indisponível — o texto do
    relatório atual NUNCA substitui o snapshot.
    """
    metadata = revision.source_metadata or {}
    if not item.delivery_id or metadata.get("deliveryId") != str(item.delivery_id):
        return False, _CONTENT_UNAVAILABLE
    try:
        context = await report_delivery_service.load_fixed_delivery_context(
            db, item.delivery_id
        )
        document = report_delivery_service.resolve_fixed_document(context)
    except report_delivery_service.FixedSnapshotUnavailableError:
        return False, _CONTENT_UNAVAILABLE
    if (
        context.delivery.patient_id != portal.patient_id
        or context.delivery.professional_id != portal.owner_professional_id
    ):
        return False, _CONTENT_UNAVAILABLE
    if document.report_type != FAMILY_REPORT_TYPE:
        return False, _CONTENT_UNAVAILABLE
    if metadata.get("contentHash") != document.content_hash:
        return False, _CONTENT_UNAVAILABLE
    return True, None


async def _public_item_summary(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    item: FamilyPortalItem,
    revision: FamilyPortalItemRevision,
) -> PublicItemSummary:
    content = revision.content or {}
    session_on: date | None = None
    family_status: str | None = None
    available = True
    if item.kind == "session_summary":
        raw_date = (revision.source_metadata or {}).get("sessionDate")
        if isinstance(raw_date, str) and raw_date:
            try:
                session_on = date.fromisoformat(raw_date)
            except ValueError:
                session_on = None
    elif item.kind == "goal":
        family_status = content.get("familyStatus")
    elif item.kind == "material":
        available, _ = await _published_material_availability(
            db, portal, item, revision
        )
    elif item.kind == "report":
        available, _ = await _published_report_availability(
            db, portal, item, revision
        )
    return PublicItemSummary(
        id=str(item.id),
        kind=item.kind,
        title=str(content.get("title") or ""),
        published_at=_as_utc(item.published_at),
        session_on=session_on,
        family_status=family_status,
        available=available,
    )


async def _published_revision(
    db: AsyncSession,
    item: FamilyPortalItem,
    *,
    refresh: bool = False,
) -> FamilyPortalItemRevision | None:
    """Resolve o par exato (item, published_version); ponteiro órfão falha."""
    if item.published_version is None:
        return None
    query = select(FamilyPortalItemRevision).where(
        FamilyPortalItemRevision.item_id == item.id,
        FamilyPortalItemRevision.version == item.published_version,
    )
    if refresh:
        # Revalidação pós-I/O: relê a linha real, não a cópia do identity map.
        query = query.execution_options(populate_existing=True)
    return await db.scalar(query)


async def get_published_item_rows(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    recipient: FamilyPortalRecipient,
    item_id: UUID,
    refresh: bool = False,
) -> tuple[FamilyPortalItem, FamilyPortalItemRevision]:
    """Item publicado no público do destinatário + revisão exata; 404 fora.

    A busca contém portal E audiência (nunca só o UUID) e o ponteiro órfão
    falha fechado. Compartilhado entre o detalhe público e a entrega de
    arquivos (``family_portal_files``).
    """
    query = (
        select(FamilyPortalItem)
        .join(
            FamilyPortalItemAudience,
            FamilyPortalItemAudience.item_id == FamilyPortalItem.id,
        )
        .where(
            FamilyPortalItem.portal_id == portal.id,
            FamilyPortalItem.id == item_id,
            FamilyPortalItem.status == "published",
            FamilyPortalItemAudience.recipient_id == recipient.id,
        )
    )
    if refresh:
        query = query.execution_options(populate_existing=True)
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PUBLIC_ITEM_NOT_FOUND_DETAIL,
        )
    revision = await _published_revision(db, item, refresh=refresh)
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PUBLIC_ITEM_NOT_FOUND_DETAIL,
        )
    return item, revision


async def list_published_items(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    recipient: FamilyPortalRecipient,
    kind: str,
    page: int,
    limit: int,
) -> PaginatedResponse[PublicItemSummary]:
    """Listagem pública/preview: só publicado, no público do destinatário."""
    now = utcnow()
    base = _published_items_select(
        portal=portal, recipient=recipient, kind=kind, now=now
    )
    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (
        await db.execute(
            base.add_columns(FamilyPortalItemRevision)
            .order_by(
                FamilyPortalItem.published_at.desc(),
                FamilyPortalItem.id.desc(),
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).all()
    items = [
        await _public_item_summary(
            db, portal=portal, item=item, revision=revision
        )
        for item, revision in rows
    ]
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def get_published_item(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    recipient: FamilyPortalRecipient,
    item_id: UUID,
) -> PublicItemDetail:
    """Detalhe público; item fora do público do destinatário vira 404."""
    item, revision = await get_published_item_rows(
        db, portal=portal, recipient=recipient, item_id=item_id
    )
    content = revision.content or {}
    published_at = _as_utc(item.published_at)
    if item.kind == "session_summary":
        session_on: date | None = None
        raw_date = (revision.source_metadata or {}).get("sessionDate")
        if isinstance(raw_date, str) and raw_date:
            try:
                session_on = date.fromisoformat(raw_date)
            except ValueError:
                session_on = None
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=True,
            body=str(content.get("body") or ""),
            session_on=session_on,
        )
    if item.kind == "goal":
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=True,
            body=str(content.get("body") or ""),
            family_status=content.get("familyStatus"),
        )
    if item.kind == "notice":
        expires_at = (
            _as_utc(revision.expires_at)
            if revision.expires_at is not None
            else None
        )
        if expires_at is not None and expires_at <= utcnow():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PUBLIC_ITEM_NOT_FOUND_DETAIL,
            )
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=True,
            body=str(content.get("body") or ""),
            expires_at=expires_at,
        )
    if item.kind == "material":
        available, _ = await _published_material_availability(
            db, portal, item, revision
        )
        if not available:
            return PublicItemDetail(
                id=str(item.id),
                kind=item.kind,
                title=str(content.get("title") or ""),
                published_at=published_at,
                available=False,
                unavailable_reason=_CONTENT_UNAVAILABLE,
            )
        metadata = revision.source_metadata or {}
        raw_size = metadata.get("sizeBytes")
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=True,
            instructions=str(content.get("instructions") or ""),
            attribution=str(metadata.get("attribution") or ""),
            content_type=(
                metadata.get("contentType")
                if isinstance(metadata.get("contentType"), str)
                else None
            ),
            size_bytes=raw_size if isinstance(raw_size, int) else None,
        )
    # report: o texto vem SEMPRE do snapshot F1 (nunca do rascunho/relatório).
    available, _ = await _published_report_availability(
        db, portal, item, revision
    )
    if not available:
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=False,
            unavailable_reason=_CONTENT_UNAVAILABLE,
        )
    try:
        context = await report_delivery_service.load_fixed_delivery_context(
            db, item.delivery_id
        )
        document = report_delivery_service.resolve_fixed_document(context)
    except report_delivery_service.FixedSnapshotUnavailableError:
        return PublicItemDetail(
            id=str(item.id),
            kind=item.kind,
            title=str(content.get("title") or ""),
            published_at=published_at,
            available=False,
            unavailable_reason=_CONTENT_UNAVAILABLE,
        )
    return PublicItemDetail(
        id=str(item.id),
        kind=item.kind,
        title=str(content.get("title") or ""),
        published_at=published_at,
        available=True,
        content=document.content,
        report_date=document.report_date,
        report_version=document.report_version,
        content_hash=document.content_hash,
        patient_name=document.patient_name,
        professional_name=document.professional_name,
        professional_council=document.professional_council,
    )


async def build_recipient_preview(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    recipient_id: UUID,
    kind: str,
    page: int,
    limit: int,
) -> PaginatedResponse[PublicItemSummary]:
    """GET P/preview: MESMA projeção pública, calculada para o destinatário.

    Só conteúdo publicado e efetivamente acessível a ele; não emite token,
    não amplia audiência e não simula autorização vencida.
    """
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _conflict(_PORTAL_MISSING_MESSAGE)
    if not portal.enabled:
        raise _conflict(_PORTAL_DISABLED_MESSAGE)
    if patient.status == "inativo":
        raise _conflict(_PATIENT_INACTIVE_MESSAGE)
    recipient = await _require_recipient(db, portal, recipient_id)
    if not recipient.active:
        raise _conflict(_PREVIEW_RECIPIENT_INACTIVE)
    current_event = await _current_authorization_event(db, recipient)
    if current_event is None:
        raise _conflict(_PREVIEW_RECIPIENT_INACTIVE)
    return await list_published_items(
        db,
        portal=portal,
        recipient=recipient,
        kind=kind,
        page=page,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# Helper consumido pelas retiradas da onda 1 (access service) — flush only
# --------------------------------------------------------------------------- #


async def strip_recipient_from_items(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    recipient_id: UUID,
    actor: Professional,
    reason: str,
) -> int:
    """Remove o destinatário dos públicos vigentes E rascunhos de todos os
    itens não retirados do portal; NUNCA apaga revisões.

    Roda na MESMA transação do caller legado (``withdraw_recipient`` /
    ``withdraw_recipient_for_caregiver``), apenas com flush. Versiona cada
    item alterado para invalidar publicação concorrente preparada antes da
    retirada e registra um evento por item mudado. Devolve quantos itens
    foram alterados.
    """
    items = (
        (
            await db.execute(
                select(FamilyPortalItem)
                .where(
                    FamilyPortalItem.portal_id == portal.id,
                    FamilyPortalItem.status.in_(("draft", "published")),
                )
                .order_by(FamilyPortalItem.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return 0
    by_id = {item.id: item for item in items}
    changed: set[UUID] = set()
    audience_rows = (
        (
            await db.execute(
                select(FamilyPortalItemAudience)
                .join(
                    FamilyPortalItem,
                    FamilyPortalItem.id == FamilyPortalItemAudience.item_id,
                )
                .where(
                    FamilyPortalItem.portal_id == portal.id,
                    FamilyPortalItemAudience.recipient_id == recipient_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in audience_rows:
        item = by_id.get(row.item_id)
        await db.delete(row)
        if item is not None:
            changed.add(item.id)
    for item in items:
        stored = [str(value) for value in (item.draft_recipient_ids or [])]
        if str(recipient_id) in stored:
            item.draft_recipient_ids = [
                value for value in stored if value != str(recipient_id)
            ]
            changed.add(item.id)
    for item in items:
        if item.id in changed:
            item.version += 1
            _record_event(
                db,
                portal=portal,
                actor=actor,
                event_type=EVENT_ITEM_RECIPIENT_REMOVED,
                item_id=item.id,
                recipient_id=recipient_id,
                payload={"reason": reason},
            )
    if changed:
        await db.flush()
    return len(changed)


# Reexporta o alias público do kind de fonte disponível (documentação viva).
__all__ = [
    "MAX_ITEMS_PER_PORTAL",
    "MAX_PUBLICATION_RECIPIENTS",
    "EVENT_ITEM_PUBLISHED",
    "EVENT_ITEM_WITHDRAWN",
    "EVENT_ITEM_RECIPIENT_REMOVED",
    "PUBLIC_ITEM_NOT_FOUND_DETAIL",
    "list_sources",
    "create_item",
    "list_items",
    "get_item",
    "update_item",
    "publish_item",
    "withdraw_item",
    "remove_item_recipient",
    "list_item_revisions",
    "list_published_items",
    "get_published_item",
    "get_published_item_rows",
    "build_recipient_preview",
    "strip_recipient_from_items",
    "session_source_fingerprint",
    "goal_source_fingerprint",
    "material_source_fingerprint",
    "report_source_fingerprint",
]
