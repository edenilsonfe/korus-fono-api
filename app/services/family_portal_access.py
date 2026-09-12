"""F14 — portal da família: autorizações versionadas, grants e leitura pública.

Responsabilidades (Tarefa 1.1, §3.1–3.2/§3.4 do plano):

- administração do dono sobre um portal por paciente (habilitação/desativação
  com versões otimistas e epoch de acesso próprio);
- destinatários explícitos por ``Caregiver``: autorização append-only e
  versionada (o evento da versão corrente é a autorização vigente);
- grants com token opaco (somente hash persistido), emissão idempotente por
  destinatário, rotação transacional e revogação idempotente;
- resolução pública do header ``X-Family-Portal-Token``: QUALQUER estado
  inválido (token, portal, dono, paciente, responsável, época) vira 410
  genérico, sem enumerar o paciente e sem expor motivo;
- helpers de ciclo de vida (``invalidate_portal_for_patient``,
  ``withdraw_recipient_for_caregiver``) para o integrador conectar aos callers
  legados na tarefa 1.2 — aqui eles só fazem flush, nunca commit.

O token nunca é persistido em claro e não aparece em logs. Toda mutação
própria do portal faz commit explícito no router ANTES do 2xx; os helpers
chamados dentro de transações legadas apenas fazem flush.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.caregiver import Caregiver
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalEvent,
    FamilyPortalGrant,
    FamilyPortalRecipient,
)
from app.models.family_portal_content import FamilyPortalItem
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.family_portal import (
    PUBLIC_PORTAL_SECTIONS,
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
    PublicFamilyPortalProfessional,
    PublicFamilyPortalResponse,
)
from app.utils.token_hash import hash_token

WEB_FAMILY_PORTAL_PATH = "/responsaveis"
DEFAULT_GRANT_DAYS = 30
MIN_GRANT_DAYS = 1
MAX_GRANT_DAYS = 30
MAX_ACTIVE_RECIPIENTS = 10
# Tolerância de relógio para a declaração ``authorizedAt`` da profissional.
AUTHORIZED_AT_SKEW = timedelta(minutes=5)
# Token opaco do grant: limite defensivo do header público.
MAX_TOKEN_LENGTH = 128

FAMILY_PORTAL_LINK_GONE_MESSAGE = (
    "Este link está inválido ou indisponível. Peça um novo link à profissional."
)

EVENT_PORTAL_ENABLED = "portal_enabled"
EVENT_PORTAL_DISABLED = "portal_disabled"
EVENT_RECIPIENT_AUTHORIZED = "recipient_authorized"
EVENT_RECIPIENT_WITHDRAWN = "recipient_withdrawn"
EVENT_APPOINTMENTS_CHANGED = "appointments_changed"
EVENT_GRANT_ISSUED = "grant_issued"
EVENT_GRANT_REVOKED = "grant_revoked"

# Tipos cujo payload pode ser exposto como ``authorization`` na trilha privada.
AUTHORIZATION_EVENT_TYPES = (EVENT_RECIPIENT_AUTHORIZED, EVENT_RECIPIENT_WITHDRAWN)

_STALE_VERSION_MESSAGE = "Versão desatualizada. Atualize e tente novamente."
_RECIPIENT_NOT_FOUND = "Destinatário não encontrado"
_GRANT_NOT_FOUND = "Link da família não encontrado"
_CAREGIVER_NOT_FOUND = "Responsável não encontrado"
_PORTAL_MISSING_MESSAGE = (
    "Habilite o portal da família antes de continuar."
)
_PORTAL_DISABLED_MESSAGE = (
    "O portal da família está desabilitado. Habilite-o antes de continuar."
)
_PORTAL_FOREIGN_OWNER_MESSAGE = (
    "O portal da família pertence a outro profissional."
)
_PATIENT_INACTIVE_MESSAGE = (
    "Paciente inativo: reative o paciente antes de liberar novos acessos da família."
)
_RECIPIENT_WITHDRAWN_MESSAGE = (
    "Responsável retirado do portal. Autorize novamente para emitir um link."
)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _gone() -> HTTPException:
    """410 genérico: nunca enumera paciente, motivo financeiro ou estado interno."""
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=FAMILY_PORTAL_LINK_GONE_MESSAGE,
    )


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    )


# --------------------------------------------------------------------------- #
# Escopo e locks (ordem §3.6: profissional → paciente → portal → destinatários
# → grants; UUID crescente nos filhos)
# --------------------------------------------------------------------------- #


async def require_owned_patient(
    db: AsyncSession, patient_id: UUID, actor: Professional
) -> Patient:
    """Portal é do dono do paciente; qualquer outro caso vira 404 neutro.

    A regra de dono NÃO é substituída por acesso clínico compartilhado (care
    team não enxerga destinatários/links — decisão fechada §2).
    """
    patient = await db.get(Patient, patient_id)
    if patient is None or patient.professional_id != actor.id:
        raise _not_found("Paciente não encontrado")
    return patient


async def _lock_actor(db: AsyncSession, actor: Professional) -> Professional:
    await db.execute(
        select(Professional.id)
        .where(Professional.id == actor.id)
        .with_for_update()
    )
    await db.refresh(actor)
    return actor


async def _lock_patient(db: AsyncSession, patient: Patient) -> Patient:
    await db.execute(
        select(Patient.id)
        .where(Patient.id == patient.id)
        .with_for_update()
    )
    await db.refresh(patient)
    return patient


async def _get_portal(
    db: AsyncSession, patient_id: UUID, *, lock: bool = False
) -> FamilyPortal | None:
    query = select(FamilyPortal).where(FamilyPortal.patient_id == patient_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def _require_portal(
    db: AsyncSession,
    patient: Patient,
    actor: Professional,
    *,
    lock: bool = True,
    require_enabled: bool = True,
) -> FamilyPortal:
    portal = await _get_portal(db, patient.id, lock=lock)
    if portal is None:
        raise _conflict(_PORTAL_MISSING_MESSAGE)
    if portal.owner_professional_id != actor.id:
        raise _conflict(_PORTAL_FOREIGN_OWNER_MESSAGE)
    if require_enabled and not portal.enabled:
        raise _conflict(_PORTAL_DISABLED_MESSAGE)
    return portal


async def _require_recipient(
    db: AsyncSession,
    portal: FamilyPortal,
    recipient_id: UUID,
    *,
    lock: bool = False,
) -> FamilyPortalRecipient:
    query = select(FamilyPortalRecipient).where(
        FamilyPortalRecipient.id == recipient_id,
        FamilyPortalRecipient.portal_id == portal.id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    recipient = await db.scalar(query)
    if recipient is None:
        raise _not_found(_RECIPIENT_NOT_FOUND)
    return recipient


def _assert_patient_accepts_family_access(patient: Patient) -> None:
    if patient.status == "inativo":
        raise _conflict(_PATIENT_INACTIVE_MESSAGE)


def _record_event(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    actor: Professional,
    event_type: str,
    recipient_id: UUID | None = None,
    authorization_version: int | None = None,
    grant_id: UUID | None = None,
    item_id: UUID | None = None,
    payload: dict | None = None,
) -> FamilyPortalEvent:
    event = FamilyPortalEvent(
        portal_id=portal.id,
        recipient_id=recipient_id,
        actor_professional_id=actor.id,
        event_type=event_type,
        authorization_version=authorization_version,
        grant_id=grant_id,
        item_id=item_id,
        payload=payload or {},
        occurred_at=utcnow(),
    )
    db.add(event)
    return event


# --------------------------------------------------------------------------- #
# Leitura e projeções
# --------------------------------------------------------------------------- #


def recipient_label(caregiver: Caregiver | None) -> str:
    """Rótulo do destinatário; caregiver removido usa neutro (sem histórico)."""
    if caregiver is None:
        return "Responsável"
    relation = (caregiver.relation or "").strip()
    name = (caregiver.name or "").strip()
    return f"{name} ({relation})" if relation else name


def grant_matches_current_epoch(
    grant: FamilyPortalGrant,
    owner: Professional,
    portal: FamilyPortal | None,
) -> bool:
    """Verificação de epoch F14: conta (dono) e portal precisam bater.

    Usada pelo resolver público (mismatch → 410) e pela classificação de
    metadados (mismatch → ``invalidated``). A versão de autorização é checada
    por quem tem o evento corrente em mãos.
    """
    return (
        portal is not None
        and grant.owner_access_version == owner.family_portal_access_version
        and grant.portal_access_version == portal.access_version
    )


def _grant_status(
    grant: FamilyPortalGrant,
    *,
    owner: Professional,
    portal: FamilyPortal,
    current_authorization_event: FamilyPortalEvent | None,
) -> str:
    if grant.revoked_at is not None:
        return "revoked"
    if (
        current_authorization_event is None
        or grant.authorization_event_id != current_authorization_event.id
        or not grant_matches_current_epoch(grant, owner, portal)
    ):
        return "invalidated"
    if _as_utc(grant.expires_at) <= utcnow():
        return "expired"
    return "active"


async def _current_authorization_event(
    db: AsyncSession, recipient: FamilyPortalRecipient
) -> FamilyPortalEvent | None:
    """Autorização vigente é o evento da versão corrente (retirada posterior vale)."""
    if recipient.authorization_version <= 0:
        return None
    return await db.scalar(
        select(FamilyPortalEvent).where(
            FamilyPortalEvent.recipient_id == recipient.id,
            FamilyPortalEvent.authorization_version
            == recipient.authorization_version,
        )
    )


async def _current_grant(
    db: AsyncSession, recipient: FamilyPortalRecipient
) -> FamilyPortalGrant | None:
    return await db.scalar(
        select(FamilyPortalGrant).where(
            FamilyPortalGrant.recipient_id == recipient.id,
            FamilyPortalGrant.revoked_at.is_(None),
        )
    )


def _grant_response(
    grant: FamilyPortalGrant,
    *,
    owner: Professional,
    portal: FamilyPortal,
    current_authorization_event: FamilyPortalEvent | None,
) -> FamilyPortalGrantResponse:
    return FamilyPortalGrantResponse(
        id=str(grant.id),
        recipient_id=str(grant.recipient_id),
        created_at=grant.created_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        status=_grant_status(
            grant,
            owner=owner,
            portal=portal,
            current_authorization_event=current_authorization_event,
        ),
    )


async def _recipient_response(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    owner: Professional,
    recipient: FamilyPortalRecipient,
    caregiver: Caregiver | None = None,
) -> FamilyPortalRecipientResponse:
    if caregiver is None and recipient.caregiver_id is not None:
        caregiver = await db.get(Caregiver, recipient.caregiver_id)
    event = await _current_authorization_event(db, recipient)
    grant = await _current_grant(db, recipient)
    authorized_at: datetime | None = None
    recorded_at: datetime | None = None
    if event is not None:
        recorded_at = _as_utc(event.occurred_at)
        raw_authorized_at = (event.payload or {}).get("authorizedAt")
        if isinstance(raw_authorized_at, str) and raw_authorized_at:
            try:
                authorized_at = datetime.fromisoformat(raw_authorized_at)
            except ValueError:
                authorized_at = None
    return FamilyPortalRecipientResponse(
        id=str(recipient.id),
        caregiver_id=str(recipient.caregiver_id) if recipient.caregiver_id else None,
        recipient_label=recipient_label(caregiver),
        active=recipient.active,
        appointments_enabled=recipient.appointments_enabled,
        version=recipient.version,
        authorization_version=recipient.authorization_version,
        authorized_at=authorized_at,
        recorded_at=recorded_at,
        current_grant=(
            _grant_response(
                grant,
                owner=owner,
                portal=portal,
                current_authorization_event=event,
            )
            if grant is not None
            else None
        ),
    )


def _event_response(event: FamilyPortalEvent) -> FamilyPortalEventResponse:
    payload = event.payload or {}
    authorization = payload if event.event_type in AUTHORIZATION_EVENT_TYPES else None
    reason = payload.get("reason")
    return FamilyPortalEventResponse(
        id=str(event.id),
        type=event.event_type,
        occurred_at=_as_utc(event.occurred_at),
        actor_professional_id=str(event.actor_professional_id),
        recipient_id=str(event.recipient_id) if event.recipient_id else None,
        item_id=str(event.item_id) if event.item_id else None,
        grant_id=str(event.grant_id) if event.grant_id else None,
        reason=reason if isinstance(reason, str) else None,
        authorization=authorization,
    )


async def _published_item_count(db: AsyncSession, portal: FamilyPortal) -> int:
    """Itens publicados do portal (camada editorial M2)."""
    count = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalItem)
        .where(
            FamilyPortalItem.portal_id == portal.id,
            FamilyPortalItem.status == "published",
        )
    )
    return int(count or 0)


async def _portal_response(
    db: AsyncSession, portal: FamilyPortal, *, patient_id: UUID
) -> FamilyPortalResponse:
    recipient_count = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalRecipient)
        .where(FamilyPortalRecipient.portal_id == portal.id)
    )
    return FamilyPortalResponse(
        id=str(portal.id),
        patient_id=str(patient_id),
        enabled=portal.enabled,
        version=portal.version,
        recipient_count=int(recipient_count or 0),
        published_item_count=await _published_item_count(db, portal),
    )


def _disabled_portal_response(*, patient_id: UUID) -> FamilyPortalResponse:
    return FamilyPortalResponse(
        id=None,
        patient_id=str(patient_id),
        enabled=False,
        version=1,
        recipient_count=0,
        published_item_count=0,
    )


# --------------------------------------------------------------------------- #
# §3.1 — administração do portal
# --------------------------------------------------------------------------- #


async def get_portal(
    db: AsyncSession, patient_id: UUID, actor: Professional
) -> FamilyPortalResponse:
    """GET P; leitura nunca cria portal nem autorização."""
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        return _disabled_portal_response(patient_id=patient.id)
    return await _portal_response(db, portal, patient_id=patient.id)


async def enable_portal(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    body: FamilyPortalEnableRequest,
) -> FamilyPortalResponse:
    """PUT P {enabled:true}: cria e habilita na mesma transação quando ausente.

    O lock do paciente serializa duas primeiras ativações; já habilitado na
    versão atual é idempotente (sem linha/evento duplicado).
    """
    patient = await require_owned_patient(db, patient_id, actor)
    _assert_patient_accepts_family_access(patient)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is not None and portal.owner_professional_id != actor.id:
        raise _conflict(_PORTAL_FOREIGN_OWNER_MESSAGE)
    if portal is None:
        if body.expected_version != 1:
            raise _conflict(_STALE_VERSION_MESSAGE)
        portal = FamilyPortal(
            patient_id=patient.id,
            owner_professional_id=actor.id,
            enabled=False,
            access_version=0,
            version=1,
        )
        db.add(portal)
        await db.flush()
        portal.enabled = True
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_PORTAL_ENABLED,
            payload={"version": portal.version},
        )
        await db.flush()
    else:
        if body.expected_version != portal.version:
            raise _conflict(_STALE_VERSION_MESSAGE)
        if not portal.enabled:
            portal.enabled = True
            portal.version += 1
            _record_event(
                db,
                portal=portal,
                actor=actor,
                event_type=EVENT_PORTAL_ENABLED,
                payload={"version": portal.version},
            )
            await db.flush()
    return await _portal_response(db, portal, patient_id=patient.id)


async def disable_portal(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
) -> FamilyPortalResponse:
    """POST P/disable: ação protetiva, idempotente e disponível em read-only.

    Revoga todos os grants não revogados e incrementa ``access_version`` quando
    havia portal habilitado ou acessos a invalidar (links antigos morrem para
    sempre — reativar não ressuscita nada).
    """
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None or portal.owner_professional_id != actor.id:
        return _disabled_portal_response(patient_id=patient.id)
    revoked = await _revoke_grants_for_recipients(
        db, portal=portal, actor=actor, reason="portal_disabled"
    )
    had_access = portal.enabled or revoked > 0
    if not had_access:
        # Repetir é inócuo: sem novo evento e sem nova versão.
        return await _portal_response(db, portal, patient_id=patient.id)
    portal.enabled = False
    portal.access_version += 1
    portal.version += 1
    _record_event(
        db,
        portal=portal,
        actor=actor,
        event_type=EVENT_PORTAL_DISABLED,
        payload={"reason": "professional_disabled", "revokedGrants": revoked},
    )
    await db.flush()
    return await _portal_response(db, portal, patient_id=patient.id)


async def _revoke_grants_for_recipients(
    db: AsyncSession,
    *,
    portal: FamilyPortal,
    actor: Professional,
    reason: str,
    recipient_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> int:
    """Revoga grants não revogados dos destinatários do portal (com evento).

    Ordem §3.6: destinatários (UUID crescente) antes dos grants (UUID
    crescente); devolve quantos grants foram efetivamente revogados.
    """
    query = select(FamilyPortalRecipient).where(
        FamilyPortalRecipient.portal_id == portal.id
    )
    if recipient_ids is not None:
        query = query.where(FamilyPortalRecipient.id.in_(recipient_ids))
    recipients = (
        (await db.execute(query.order_by(FamilyPortalRecipient.id).with_for_update()))
        .scalars()
        .all()
    )
    if not recipients:
        return 0
    grants = (
        (
            await db.execute(
                select(FamilyPortalGrant)
                .where(
                    FamilyPortalGrant.recipient_id.in_(
                        [recipient.id for recipient in recipients]
                    ),
                    FamilyPortalGrant.revoked_at.is_(None),
                )
                .order_by(FamilyPortalGrant.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = now or utcnow()
    for grant in grants:
        grant.revoked_at = now
        grant.revoked_by_professional_id = actor.id
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_GRANT_REVOKED,
            recipient_id=grant.recipient_id,
            grant_id=grant.id,
            payload={"reason": reason},
        )
    if grants:
        await db.flush()
    return len(grants)


# --------------------------------------------------------------------------- #
# §3.1 — destinatários
# --------------------------------------------------------------------------- #


def _normalized_authorization(authorization) -> dict:
    """Payload append-only do evento de autorização (sem dado clínico)."""
    authorized_at = authorization.authorized_at
    if authorized_at.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe a data da autorização com fuso horário",
        )
    if authorized_at > utcnow() + AUTHORIZED_AT_SKEW:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A data da autorização da família não pode estar no futuro",
        )
    return {
        "authorizedAt": authorized_at.astimezone(UTC).isoformat(),
        "reference": authorization.reference,
        "reviewed": True,
    }


async def list_recipients(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalRecipientResponse]:
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        return PaginatedResponse(items=[], total=0, page=page, limit=limit)
    total = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalRecipient)
        .where(FamilyPortalRecipient.portal_id == portal.id)
    )
    rows = (
        (
            await db.execute(
                select(FamilyPortalRecipient)
                .where(FamilyPortalRecipient.portal_id == portal.id)
                .order_by(
                    FamilyPortalRecipient.created_at,
                    FamilyPortalRecipient.id,
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        await _recipient_response(db, portal=portal, owner=actor, recipient=row)
        for row in rows
    ]
    return PaginatedResponse(
        items=items, total=int(total or 0), page=page, limit=limit
    )


async def upsert_recipient(
    db: AsyncSession,
    patient_id: UUID,
    caregiver_id: UUID,
    actor: Professional,
    body: FamilyPortalRecipientUpsertRequest,
) -> FamilyPortalRecipientResponse:
    """PUT P/recipients/{caregiver_id}: autorização explícita e versionada.

    Novo destinatário nasce ativo com autorização v1; mudança efetiva de
    autorização revoga o grant anterior (emitir outro é ação explícita).
    """
    patient = await require_owned_patient(db, patient_id, actor)
    _assert_patient_accepts_family_access(patient)
    authorization_payload = _normalized_authorization(body.family_authorization)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_portal(db, patient, actor)
    caregiver = await db.scalar(
        select(Caregiver).where(
            Caregiver.id == caregiver_id,
            Caregiver.patient_id == patient.id,
        )
    )
    if caregiver is None:
        raise _not_found(_CAREGIVER_NOT_FOUND)
    recipient = await db.scalar(
        select(FamilyPortalRecipient)
        .where(
            FamilyPortalRecipient.portal_id == portal.id,
            FamilyPortalRecipient.caregiver_id == caregiver.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if recipient is None:
        if body.expected_version is not None:
            raise _conflict("Responsável ainda não autorizado neste portal.")
        active_count = await db.scalar(
            select(func.count())
            .select_from(FamilyPortalRecipient)
            .where(
                FamilyPortalRecipient.portal_id == portal.id,
                FamilyPortalRecipient.active.is_(True),
            )
        )
        if int(active_count or 0) >= MAX_ACTIVE_RECIPIENTS:
            raise _unprocessable(
                "Limite de 10 responsáveis ativos por portal. Retire um "
                "destinatário para liberar espaço."
            )
        recipient = FamilyPortalRecipient(
            portal_id=portal.id,
            caregiver_id=caregiver.id,
            active=True,
            appointments_enabled=body.appointments_enabled,
            authorization_version=1,
            version=1,
        )
        db.add(recipient)
        await db.flush()
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_RECIPIENT_AUTHORIZED,
            recipient_id=recipient.id,
            authorization_version=recipient.authorization_version,
            payload=authorization_payload,
        )
        await db.flush()
    else:
        if body.expected_version is None:
            raise _conflict(
                "Informe a versão atual do destinatário para salvar alterações."
            )
        if body.expected_version != recipient.version:
            raise _conflict(_STALE_VERSION_MESSAGE)
        current_event = await _current_authorization_event(db, recipient)
        authorization_changed = (
            current_event is None or (current_event.payload or {}) != authorization_payload
        )
        settings_changed = (
            recipient.appointments_enabled != body.appointments_enabled
        )
        if not authorization_changed and not settings_changed:
            # Replay com a versão corrente e payload idêntico não fabrica evento.
            return await _recipient_response(
                db, portal=portal, owner=actor, recipient=recipient, caregiver=caregiver
            )
        if authorization_changed:
            recipient.authorization_version += 1
            recipient.active = True  # nova autorização reativa o retirado
            _record_event(
                db,
                portal=portal,
                actor=actor,
                event_type=EVENT_RECIPIENT_AUTHORIZED,
                recipient_id=recipient.id,
                authorization_version=recipient.authorization_version,
                payload=authorization_payload,
            )
            await _revoke_grants_for_recipients(
                db,
                portal=portal,
                actor=actor,
                reason="authorization_changed",
                recipient_ids=[recipient.id],
            )
        if settings_changed:
            recipient.appointments_enabled = body.appointments_enabled
            _record_event(
                db,
                portal=portal,
                actor=actor,
                event_type=EVENT_APPOINTMENTS_CHANGED,
                recipient_id=recipient.id,
                payload={"enabled": body.appointments_enabled},
            )
        recipient.version += 1
        await db.flush()
    return await _recipient_response(
        db, portal=portal, owner=actor, recipient=recipient, caregiver=caregiver
    )


async def update_recipient_settings(
    db: AsyncSession,
    patient_id: UUID,
    recipient_id: UUID,
    actor: Professional,
    body: FamilyPortalRecipientSettingsRequest,
) -> FamilyPortalRecipientResponse:
    """PATCH P/recipients/{recipient_id}: só agenda, com versão otimista."""
    patient = await require_owned_patient(db, patient_id, actor)
    _assert_patient_accepts_family_access(patient)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_portal(db, patient, actor)
    recipient = await _require_recipient(db, portal, recipient_id, lock=True)
    if body.expected_version != recipient.version:
        raise _conflict(_STALE_VERSION_MESSAGE)
    if not recipient.active:
        raise _conflict(_RECIPIENT_WITHDRAWN_MESSAGE)
    if recipient.appointments_enabled != body.appointments_enabled:
        recipient.appointments_enabled = body.appointments_enabled
        recipient.version += 1
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_APPOINTMENTS_CHANGED,
            recipient_id=recipient.id,
            payload={"enabled": body.appointments_enabled},
        )
        await db.flush()
    return await _recipient_response(
        db, portal=portal, owner=actor, recipient=recipient
    )


async def disable_recipient_appointments(
    db: AsyncSession, patient_id: UUID, recipient_id: UUID, actor: Professional
) -> None:
    """DELETE P/recipients/{recipient_id}/appointments: idempotente e protetivo."""
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_portal(db, patient, actor, require_enabled=False)
    recipient = await _require_recipient(db, portal, recipient_id, lock=True)
    if recipient.appointments_enabled:
        recipient.appointments_enabled = False
        recipient.version += 1
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_APPOINTMENTS_CHANGED,
            recipient_id=recipient.id,
            payload={"enabled": False},
        )
        await db.flush()


async def withdraw_recipient(
    db: AsyncSession,
    patient_id: UUID,
    recipient_id: UUID,
    actor: Professional,
    body: FamilyPortalWithdrawRequest,
) -> FamilyPortalRecipientResponse:
    """POST P/recipients/{recipient_id}/withdraw: retirada protetiva idempotente.

    Avança ``authorization_version``, revoga grants e tira o destinatário dos
    públicos vigentes e rascunhos de todos os itens editoriais (sem apagar
    revisões). Repetir não duplica evento nem versão.
    """
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _not_found(_RECIPIENT_NOT_FOUND)
    recipient = await _require_recipient(db, portal, recipient_id, lock=True)
    if recipient.active:
        now = utcnow()
        recipient.active = False
        recipient.authorization_version += 1
        recipient.version += 1
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_RECIPIENT_WITHDRAWN,
            recipient_id=recipient.id,
            authorization_version=recipient.authorization_version,
            payload={"reason": body.reason},
        )
        await _revoke_grants_for_recipients(
            db,
            portal=portal,
            actor=actor,
            reason="recipient_withdrawn",
            recipient_ids=[recipient.id],
            now=now,
        )
        # Onda 2: remove o destinatário também das audiências vigentes e dos
        # rascunhos dos itens (revisões preservadas). Import tardio evita o
        # ciclo de módulos entre os serviços F14.
        from app.services.family_portal_content import (
            strip_recipient_from_items,
        )

        await strip_recipient_from_items(
            db,
            portal=portal,
            recipient_id=recipient.id,
            actor=actor,
            reason="recipient_withdrawn",
        )
        await db.flush()
    return await _recipient_response(
        db, portal=portal, owner=actor, recipient=recipient
    )


# --------------------------------------------------------------------------- #
# §3.1 — trilha de eventos
# --------------------------------------------------------------------------- #


async def list_events(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalEventResponse]:
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        return PaginatedResponse(items=[], total=0, page=page, limit=limit)
    total = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalEvent)
        .where(FamilyPortalEvent.portal_id == portal.id)
    )
    rows = (
        (
            await db.execute(
                select(FamilyPortalEvent)
                .where(FamilyPortalEvent.portal_id == portal.id)
                .order_by(
                    FamilyPortalEvent.occurred_at.desc(),
                    FamilyPortalEvent.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PaginatedResponse(
        items=[_event_response(row) for row in rows],
        total=int(total or 0),
        page=page,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# §3.2 — grants: listagem, emissão/rotação e revogação
# --------------------------------------------------------------------------- #


async def list_grants(
    db: AsyncSession,
    patient_id: UUID,
    recipient_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[FamilyPortalGrantResponse]:
    """Metadados dos grants, sem token/hash/URL; status inclui ``invalidated``."""
    patient = await require_owned_patient(db, patient_id, actor)
    portal = await _get_portal(db, patient.id)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _not_found(_RECIPIENT_NOT_FOUND)
    recipient = await _require_recipient(db, portal, recipient_id)
    total = await db.scalar(
        select(func.count())
        .select_from(FamilyPortalGrant)
        .where(FamilyPortalGrant.recipient_id == recipient.id)
    )
    rows = (
        (
            await db.execute(
                select(FamilyPortalGrant)
                .where(FamilyPortalGrant.recipient_id == recipient.id)
                .order_by(
                    FamilyPortalGrant.created_at.desc(),
                    FamilyPortalGrant.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    current_event = await _current_authorization_event(db, recipient)
    return PaginatedResponse(
        items=[
            _grant_response(
                row,
                owner=actor,
                portal=portal,
                current_authorization_event=current_event,
            )
            for row in rows
        ],
        total=int(total or 0),
        page=page,
        limit=limit,
    )


async def issue_grant(
    db: AsyncSession,
    patient_id: UUID,
    recipient_id: UUID,
    actor: Professional,
    body: FamilyPortalGrantIssueRequest,
) -> tuple[FamilyPortalGrant, str]:
    """Emite/rotaciona o link do destinatário; devolve (grant, url com fragmento).

    Rotação: revoga o anterior + insere o sucessor + evento na MESMA transação;
    o router commita ANTES de devolver a URL. Corrida de duas emissões: uma
    vence, a outra recebe 409 (nunca um sucesso de link imediatamente perdido).
    """
    patient = await require_owned_patient(db, patient_id, actor)
    _assert_patient_accepts_family_access(patient)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _require_portal(db, patient, actor)
    recipient = await _require_recipient(db, portal, recipient_id, lock=True)
    if not recipient.active:
        raise _conflict(_RECIPIENT_WITHDRAWN_MESSAGE)
    if recipient.caregiver_id is None:
        raise _conflict(_RECIPIENT_WITHDRAWN_MESSAGE)
    caregiver = await db.get(Caregiver, recipient.caregiver_id)
    if caregiver is None or caregiver.patient_id != patient.id:
        raise _conflict(_RECIPIENT_WITHDRAWN_MESSAGE)
    if body.expected_recipient_version != recipient.version:
        raise _conflict(_STALE_VERSION_MESSAGE)
    current_event = await _current_authorization_event(db, recipient)
    if current_event is None:
        raise _conflict(_RECIPIENT_WITHDRAWN_MESSAGE)

    now = utcnow()
    previous = await db.scalar(
        select(FamilyPortalGrant)
        .where(
            FamilyPortalGrant.recipient_id == recipient.id,
            FamilyPortalGrant.revoked_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous is not None:
        if body.rotate_from_grant_id is None or body.rotate_from_grant_id != previous.id:
            raise _conflict(
                "Já existe um link ativo para este responsável. Informe o "
                "link atual para rotacionar."
            )
        previous.revoked_at = now
        previous.revoked_by_professional_id = actor.id
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_GRANT_REVOKED,
            recipient_id=recipient.id,
            grant_id=previous.id,
            payload={"reason": "rotated"},
        )
        # Libera o índice parcial único antes do INSERT do sucessor.
        await db.flush()
    elif body.rotate_from_grant_id is not None:
        raise _conflict(
            "Não há link ativo para rotacionar; rotateFromGrantId deve ser "
            "nulo na primeira emissão."
        )

    raw_token = secrets.token_urlsafe(32)
    grant = FamilyPortalGrant(
        recipient_id=recipient.id,
        authorization_event_id=current_event.id,
        token_hash=hash_token(raw_token),
        owner_access_version=actor.family_portal_access_version,
        portal_access_version=portal.access_version,
        created_by_professional_id=actor.id,
        expires_at=now + timedelta(days=body.expires_in_days),
        revoked_at=None,
    )
    db.add(grant)
    try:
        await db.flush()
    except IntegrityError as exc:  # índice parcial: corrida perdida → 409
        await db.rollback()
        raise _conflict(
            "Já existe um link ativo para este responsável. Recarregue os "
            "metadados e tente novamente."
        ) from exc
    _record_event(
        db,
        portal=portal,
        actor=actor,
        event_type=EVENT_GRANT_ISSUED,
        recipient_id=recipient.id,
        grant_id=grant.id,
        payload={"recipientVersion": recipient.version},
    )
    await db.flush()
    await db.refresh(grant)
    base = get_settings().billing_frontend_base_url
    url = f"{base}{WEB_FAMILY_PORTAL_PATH}#token={raw_token}"
    return grant, url


async def grant_issued_response(
    db: AsyncSession,
    *,
    grant: FamilyPortalGrant,
    owner: Professional,
    url: str,
) -> FamilyPortalGrantIssuedResponse:
    """Resposta 201 da emissão: metadados + URL com fragmento (uma única vez)."""
    recipient = await db.get(FamilyPortalRecipient, grant.recipient_id)
    portal = (
        await db.get(FamilyPortal, recipient.portal_id)
        if recipient is not None
        else None
    )
    current_event = (
        await _current_authorization_event(db, recipient)
        if recipient is not None
        else None
    )
    metadata = _grant_response(
        grant,
        owner=owner,
        portal=portal,
        current_authorization_event=current_event,
    )
    return FamilyPortalGrantIssuedResponse(url=url, **metadata.model_dump())


async def revoke_grant(
    db: AsyncSession,
    patient_id: UUID,
    recipient_id: UUID,
    grant_id: UUID,
    actor: Professional,
) -> None:
    """DELETE P/recipients/{recipient_id}/grants/{grant_id}; repetir continua 204.

    Revogar não retira conteúdo nem consentimento — só mata o link.
    """
    patient = await require_owned_patient(db, patient_id, actor)
    actor = await _lock_actor(db, actor)
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None or portal.owner_professional_id != actor.id:
        raise _not_found(_GRANT_NOT_FOUND)
    recipient = await _require_recipient(db, portal, recipient_id, lock=True)
    grant = await db.scalar(
        select(FamilyPortalGrant)
        .where(
            FamilyPortalGrant.id == grant_id,
            FamilyPortalGrant.recipient_id == recipient.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is None:
        raise _not_found(_GRANT_NOT_FOUND)
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        grant.revoked_by_professional_id = actor.id
        _record_event(
            db,
            portal=portal,
            actor=actor,
            event_type=EVENT_GRANT_REVOKED,
            recipient_id=recipient.id,
            grant_id=grant.id,
            payload={"reason": "revoked"},
        )
        await db.flush()


# --------------------------------------------------------------------------- #
# §3.4 — resolução pública (410 genérico para TODOS os estados inválidos)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublicFamilyPortalContext:
    """Grant do header público, revalidado (portal, dono, paciente, autorização)."""

    grant: FamilyPortalGrant
    portal: FamilyPortal
    patient: Patient
    owner: Professional
    recipient: FamilyPortalRecipient
    caregiver: Caregiver


async def resolve_public_context(
    db: AsyncSession, raw_token: str | None, *, lock: bool = False
) -> PublicFamilyPortalContext:
    """Resolve o header ``X-Family-Portal-Token`` para o portal da família.

    Qualquer estado inválido — token ausente/malformado/desconhecido/revogado/
    expirado, portal desabilitado, paciente inativo/removido, responsável
    removido/retirado/trocado, dono desativado ou época (conta/portal/
    autorização) divergente — vira 410 com o MESMO corpo genérico.

    Com ``lock=True`` adquire os locks na ordem §3.6 (profissional dono →
    paciente → portal → destinatário → grant) e revalida DEPOIS do lock, para
    que revogação/retirada no intervalo não deixe passar um commit.
    """
    raw = (raw_token or "").strip()
    if not raw or len(raw) > MAX_TOKEN_LENGTH:
        raise _gone()
    grant = await db.scalar(
        select(FamilyPortalGrant).where(
            FamilyPortalGrant.token_hash == hash_token(raw)
        )
    )
    if grant is None:
        raise _gone()
    recipient = await db.get(FamilyPortalRecipient, grant.recipient_id)
    portal = await db.get(FamilyPortal, recipient.portal_id) if recipient else None
    if recipient is None or portal is None:
        raise _gone()
    patient = await db.get(Patient, portal.patient_id)
    owner = (
        await db.get(Professional, patient.professional_id)
        if patient is not None
        else None
    )

    if lock:
        if owner is not None:
            await db.execute(
                select(Professional.id)
                .where(Professional.id == owner.id)
                .with_for_update()
            )
        if patient is not None:
            await db.execute(
                select(Patient.id)
                .where(Patient.id == patient.id)
                .with_for_update()
            )
        await db.execute(
            select(FamilyPortal.id)
            .where(FamilyPortal.id == portal.id)
            .with_for_update()
        )
        await db.execute(
            select(FamilyPortalRecipient.id)
            .where(FamilyPortalRecipient.id == recipient.id)
            .with_for_update()
        )
        await db.execute(
            select(FamilyPortalGrant.id)
            .where(FamilyPortalGrant.id == grant.id)
            .with_for_update()
        )
        # Reconsulta pós-lock: o estado pode ter mudado enquanto esperávamos.
        grant = await db.scalar(
            select(FamilyPortalGrant)
            .where(FamilyPortalGrant.id == grant.id)
            .execution_options(populate_existing=True)
        )
        recipient = await db.scalar(
            select(FamilyPortalRecipient)
            .where(FamilyPortalRecipient.id == recipient.id)
            .execution_options(populate_existing=True)
        )
        portal = await db.scalar(
            select(FamilyPortal)
            .where(FamilyPortal.id == portal.id)
            .execution_options(populate_existing=True)
        )
        patient = (
            await db.scalar(
                select(Patient)
                .where(Patient.id == patient.id)
                .execution_options(populate_existing=True)
            )
            if patient is not None
            else None
        )
        owner = (
            await db.scalar(
                select(Professional)
                .where(Professional.id == owner.id)
                .execution_options(populate_existing=True)
            )
            if owner is not None
            else None
        )
    if grant is None or recipient is None or portal is None:
        raise _gone()
    if patient is None or patient.status == "inativo":
        raise _gone()
    if owner is None or owner.is_disabled:
        raise _gone()
    # Troca de dono (fluxo administrativo futuro) não transfere o portal.
    if patient.professional_id != portal.owner_professional_id:
        raise _gone()
    if not portal.enabled:
        raise _gone()
    if not recipient.active or recipient.caregiver_id is None:
        raise _gone()
    caregiver = await db.get(Caregiver, recipient.caregiver_id)
    if caregiver is None or caregiver.patient_id != patient.id:
        raise _gone()
    if grant.revoked_at is not None:
        raise _gone()
    if _as_utc(grant.expires_at) <= utcnow():
        raise _gone()
    if not grant_matches_current_epoch(grant, owner, portal):
        raise _gone()
    authorization_event = await db.get(
        FamilyPortalEvent, grant.authorization_event_id
    )
    if (
        authorization_event is None
        or authorization_event.recipient_id != recipient.id
        or authorization_event.authorization_version
        != recipient.authorization_version
    ):
        raise _gone()
    return PublicFamilyPortalContext(
        grant=grant,
        portal=portal,
        patient=patient,
        owner=owner,
        recipient=recipient,
        caregiver=caregiver,
    )


def build_public_portal_root(
    context: PublicFamilyPortalContext,
) -> PublicFamilyPortalResponse:
    """Raiz pública §3.4: identidade mínima + seções fixas da UI."""
    name = (context.patient.name or "").strip()
    return PublicFamilyPortalResponse(
        patient_first_name=name.split(" ")[0] if name else "",
        professional=PublicFamilyPortalProfessional(
            name=context.owner.name,
            council=(context.owner.council or "").strip() or None,
        ),
        expires_at=_as_utc(context.grant.expires_at),
        timezone=get_settings().clinic_timezone,
        appointments_enabled=context.recipient.appointments_enabled,
        sections=list(PUBLIC_PORTAL_SECTIONS),
    )


# --------------------------------------------------------------------------- #
# Helpers de ciclo de vida para o integrador (tarefa 1.2) — flush, nunca commit
# --------------------------------------------------------------------------- #


async def invalidate_portal_for_patient(
    db: AsyncSession,
    *,
    patient_id: UUID,
    actor: Professional,
    reason: str = "patient_inactive",
) -> int:
    """Entrada do paciente em ``inativo``: desativa e invalida para sempre.

    Chamar ANTES do commit de cancelamento de agenda, na mesma transação do
    mudança de status (lock do paciente → portal → destinatários → grants).
    Incrementa ``access_version`` quando havia portal habilitado ou grants a
    invalidar; reativar depois exige habilitação e novos links. Não commita.
    """
    patient = await db.get(Patient, patient_id)
    if patient is None:
        return 0
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None:
        return 0
    revoked = await _revoke_grants_for_recipients(
        db, portal=portal, actor=actor, reason=reason
    )
    had_access = portal.enabled or revoked > 0
    if not had_access:
        return 0
    portal.enabled = False
    portal.access_version += 1
    portal.version += 1
    _record_event(
        db,
        portal=portal,
        actor=actor,
        event_type=EVENT_PORTAL_DISABLED,
        payload={"reason": reason, "revokedGrants": revoked},
    )
    await db.flush()
    return revoked


async def withdraw_recipient_for_caregiver(
    db: AsyncSession,
    *,
    patient_id: UUID,
    caregiver_id: UUID,
    actor: Professional,
    reason: str,
    clear_caregiver_link: bool = False,
) -> int:
    """Retirada derivada do responsável (exclusão, troca de identidade/contato).

    Roda na MESMA transação do caller legado (antes do DELETE/commit) e apenas
    faz flush. Retira a autorização F14 (ativa→false, ``authorization_version``
    +1), revoga grants e registra o motivo. Nova autorização exige conferência
    nova; cadastrar outro responsável não libera histórico. Com
    ``clear_caregiver_link=True`` solta a FK antes da exclusão do caregiver.
    Devolve quantos destinatários ativos foram retirados. Não commita.
    """
    patient = await db.get(Patient, patient_id)
    if patient is None:
        return 0
    patient = await _lock_patient(db, patient)
    portal = await _get_portal(db, patient.id, lock=True)
    if portal is None:
        return 0
    recipients = (
        (
            await db.execute(
                select(FamilyPortalRecipient)
                .where(
                    FamilyPortalRecipient.portal_id == portal.id,
                    FamilyPortalRecipient.caregiver_id == caregiver_id,
                )
                .order_by(FamilyPortalRecipient.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if not recipients:
        return 0
    now = utcnow()
    withdrawn = 0
    for recipient in recipients:
        if recipient.active:
            recipient.active = False
            recipient.authorization_version += 1
            recipient.version += 1
            _record_event(
                db,
                portal=portal,
                actor=actor,
                event_type=EVENT_RECIPIENT_WITHDRAWN,
                recipient_id=recipient.id,
                authorization_version=recipient.authorization_version,
                payload={"reason": reason},
            )
            await _revoke_grants_for_recipients(
                db,
                portal=portal,
                actor=actor,
                reason=reason,
                recipient_ids=[recipient.id],
                now=now,
            )
            # Onda 2: tira o destinatário dos públicos/dos rascunhos dos itens.
            from app.services.family_portal_content import (
                strip_recipient_from_items,
            )

            await strip_recipient_from_items(
                db,
                portal=portal,
                recipient_id=recipient.id,
                actor=actor,
                reason=reason,
            )
            withdrawn += 1
        if clear_caregiver_link:
            recipient.caregiver_id = None
    await db.flush()
    return withdrawn
