"""F16 — grants do programa de casa: gates, emissão e revogação de tokens.

Responsabilidades (Tarefa 5.1):
- escopo do dono (prescrição/publicação/links) e programa no paciente;
- gate ABA para vínculos com programa de intervenção (flag do dono +
  programa ativo + autorização assistencial vigente em ``latest_consent``);
- emissão de grant com token opaco (hash persistido; token devolvido uma vez),
  validade 1–30 dias (padrão 14) limitada ao fim do programa + 7 dias;
- rotação: novo grant revoga o anterior na MESMA transação (um ativo por
  programa via índice parcial);
- revogação idempotente e revogações derivadas (responsável excluído,
  consentimento retirado), sem apagar respostas;
- resolução do header público ``X-Home-Program-Token`` para a família
  (Tarefa 5.2): qualquer estado inválido vira 410 genérico, sem enumerar o
  paciente e sem expor motivo financeiro.

O token nunca é persistido em claro e não aparece em logs.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.care_team import PatientSharingConsentEvent
from app.models.caregiver import Caregiver
from app.models.home_program import (
    HomeProgram,
    HomeProgramCheckIn,
    HomeProgramGrant,
    HomeProgramPhoto,
    HomeProgramTask,
    HomeProgramTaskResource,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.schemas.home_program import (
    HomeProgramFamilyAuthorization,
    HomeProgramGrantCreate,
    HomeProgramGrantResponse,
)
from app.services.care_team_service import latest_consent, record_access_event
from app.services.feature_flag_service import FeatureFlagService
from app.services.patient_access import (
    FEATURE_KEY,
    has_permission,
    resolve_patient_access,
)
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
    current_licenses_by_resource,
)
from app.utils.token_hash import hash_token

GRANT_PROGRAM_TAIL = timedelta(days=7)
# Tolerância de relógio para a declaração ``authorizedAt`` da família.
AUTHORIZED_AT_SKEW = timedelta(minutes=5)
WEB_HOME_PROGRAM_PATH = "/programa-de-casa"

ABA_FEATURE_MESSAGE = (
    "O vínculo com programa ABA exige a equipe assistencial "
    "multiprofissional habilitada para esta profissional"
)
ABA_CONSENT_MESSAGE = (
    "A autorização de compartilhamento precisa estar vigente "
    "para vincular programas ABA"
)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def require_owned_patient(
    db: AsyncSession, patient_id: UUID, actor: Professional
) -> Patient:
    """Prescrição/publicação/links são do dono; qualquer outro caso é 404."""
    patient = await db.get(Patient, patient_id)
    if patient is None or patient.professional_id != actor.id:
        raise _not_found("Paciente não encontrado")
    return patient


async def require_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    *,
    lock: bool = False,
) -> HomeProgram:
    query = select(HomeProgram).where(
        HomeProgram.id == program_id,
        HomeProgram.patient_id == patient_id,
    )
    if lock:
        query = query.with_for_update()
    program = await db.scalar(query)
    if program is None:
        raise _not_found("Programa de casa não encontrado")
    return program


async def aba_gate(
    db: AsyncSession, patient: Patient, owner: Professional
) -> PatientSharingConsentEvent:
    """Gate ABA: flag do dono + ``program:manage`` + consentimento vigente.

    Devolve o evento de consentimento usado — quem emite o grant o persiste.
    """
    if not await FeatureFlagService(db).is_enabled(owner, FEATURE_KEY):
        raise _conflict(ABA_FEATURE_MESSAGE)
    consent = await latest_consent(db, patient.id)
    if consent is None or consent.decision != "granted":
        raise _conflict(ABA_CONSENT_MESSAGE)
    access = await resolve_patient_access(db, patient.id, owner)
    if access is None or not has_permission(access, "program:manage"):
        raise _conflict(ABA_FEATURE_MESSAGE)
    return consent


async def program_has_aba_tasks(db: AsyncSession, program_id: UUID) -> bool:
    return (
        await db.scalar(
            select(HomeProgramTask.id)
            .where(
                HomeProgramTask.program_id == program_id,
                HomeProgramTask.intervention_program_id.is_not(None),
            )
            .limit(1)
        )
    ) is not None


def recipient_label(grant: HomeProgramGrant) -> str:
    relation = (grant.caregiver_relation_snapshot or "").strip()
    name = grant.caregiver_name_snapshot
    return f"{name} ({relation})" if relation else name


def grant_response(
    grant: HomeProgramGrant, *, url: str | None = None
) -> HomeProgramGrantResponse:
    return HomeProgramGrantResponse(
        id=str(grant.id),
        caregiver_id=str(grant.caregiver_id) if grant.caregiver_id else None,
        recipient_label=recipient_label(grant),
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        url=url,
    )


def _validate_family_authorization(authorization: HomeProgramFamilyAuthorization) -> dict:
    authorized_at = authorization.authorized_at
    if authorized_at.tzinfo is None:
        authorized_at = authorized_at.replace(tzinfo=UTC)
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


def _grant_expiry(program: HomeProgram, days: int, *, now: datetime) -> datetime:
    """Validade do grant: pedido em dias, teto = fim do programa + 7 dias."""
    cap = datetime.combine(
        program.ends_on + GRANT_PROGRAM_TAIL, time.max, tzinfo=UTC
    )
    if cap <= now:
        raise _conflict(
            "O período do programa já encerrou; não é possível gerar o link da família"
        )
    return min(now + timedelta(days=days), cap)


async def _active_grants(
    db: AsyncSession, *, program_id: UUID
) -> list[HomeProgramGrant]:
    return (
        (
            await db.execute(
                select(HomeProgramGrant)
                .where(
                    HomeProgramGrant.program_id == program_id,
                    HomeProgramGrant.revoked_at.is_(None),
                )
                .with_for_update(of=HomeProgramGrant)
            )
        )
        .scalars()
        .all()
    )


def _revoke(
    db: AsyncSession,
    grant: HomeProgramGrant,
    *,
    patient_id: UUID,
    actor: Professional,
    actor_role: str,
    now: datetime,
    clear_caregiver: bool = False,
) -> None:
    grant.revoked_at = now
    grant.revoked_by_professional_id = actor.id
    if clear_caregiver:
        grant.caregiver_id = None
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=actor_role,
        action="home_program_grant_revoked",
        resource_type="home_program_grant",
        resource_id=grant.id,
    )


async def create_grant(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    body: HomeProgramGrantCreate,
) -> tuple[HomeProgramGrant, str]:
    """Emite um link familiar (token rotativo) para um programa publicado."""
    patient = await require_owned_patient(db, patient_id, actor)
    program = await require_program(db, patient_id, program_id, lock=True)
    if program.status != "active":
        raise _conflict(
            "Publique o programa antes de gerar o link da família"
        )
    caregiver = await db.scalar(
        select(Caregiver).where(
            Caregiver.id == body.caregiver_id,
            Caregiver.patient_id == patient_id,
        )
    )
    if caregiver is None:
        raise _not_found("Responsável não encontrado")

    authorization_payload = _validate_family_authorization(
        body.family_authorization
    )
    consent: PatientSharingConsentEvent | None = None
    if await program_has_aba_tasks(db, program.id):
        consent = await aba_gate(db, patient, actor)
    else:
        latest = await latest_consent(db, patient_id)
        if latest is not None and latest.decision == "granted":
            consent = latest

    now = utcnow()
    expires_at = _grant_expiry(program, body.expires_in_days, now=now)

    # Rotação: revoga o grant anterior na MESMA transação (um ativo/programa).
    previous = await _active_grants(db, program_id=program.id)
    for grant in previous:
        grant.revoked_at = now
        grant.revoked_by_professional_id = actor.id
    if previous:
        # Libera o índice parcial antes do INSERT do novo grant.
        await db.flush()

    raw_token = secrets.token_urlsafe(32)
    grant = HomeProgramGrant(
        program_id=program.id,
        caregiver_id=caregiver.id,
        caregiver_name_snapshot=caregiver.name.strip(),
        caregiver_relation_snapshot=(caregiver.relation or "").strip() or None,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
        revoked_at=None,
        created_by_professional_id=actor.id,
        consent_event_id=consent.id if consent else None,
        family_authorization=authorization_payload,
    )
    db.add(grant)
    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role="coordinator",
        action="home_program_grant_issued",
        resource_type="home_program_grant",
        resource_id=grant.id,
    )
    await db.flush()
    base = get_settings().billing_frontend_base_url
    url = f"{base}{WEB_HOME_PROGRAM_PATH}#token={raw_token}"
    return grant, url


async def list_grants(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
) -> list[HomeProgramGrantResponse]:
    """Metadados dos grants, sem url/token, apenas ao dono."""
    await require_owned_patient(db, patient_id, actor)
    await require_program(db, patient_id, program_id)
    grants = (
        (
            await db.execute(
                select(HomeProgramGrant)
                .where(HomeProgramGrant.program_id == program_id)
                .order_by(
                    HomeProgramGrant.created_at.desc(),
                    HomeProgramGrant.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [grant_response(grant) for grant in grants]


async def revoke_grant(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    grant_id: UUID,
    actor: Professional,
) -> None:
    """Revoga um grant; repetir o DELETE continua 204 (idempotente)."""
    await require_owned_patient(db, patient_id, actor)
    program = await require_program(db, patient_id, program_id, lock=True)
    grant = await db.scalar(
        select(HomeProgramGrant)
        .where(
            HomeProgramGrant.id == grant_id,
            HomeProgramGrant.program_id == program.id,
        )
        .with_for_update()
    )
    if grant is None:
        raise _not_found("Link da família não encontrado")
    if grant.revoked_at is None:
        _revoke(
            db,
            grant,
            patient_id=patient_id,
            actor=actor,
            actor_role="coordinator",
            now=utcnow(),
        )
        await db.flush()


async def revoke_grants_for_program(
    db: AsyncSession,
    *,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    actor_role: str = "coordinator",
) -> int:
    """Arquivar revoga todos os grants ativos do programa (sem apagar nada)."""
    grants = await _active_grants(db, program_id=program_id)
    now = utcnow()
    for grant in grants:
        _revoke(
            db,
            grant,
            patient_id=patient_id,
            actor=actor,
            actor_role=actor_role,
            now=now,
        )
    if grants:
        await db.flush()
    return len(grants)


async def revoke_grants_for_caregiver(
    db: AsyncSession,
    *,
    patient_id: UUID,
    caregiver_id: UUID,
    actor: Professional,
    actor_role: str = "coordinator",
) -> int:
    """Excluir o responsável revoga seus grants antes de removê-lo.

    A FK do caregiver é SET NULL com snapshot de autoria; aqui a referência é
    limpa explicitamente junto da revogação (mesma transação do delete).
    """
    grants = (
        (
            await db.execute(
                select(HomeProgramGrant)
                .join(HomeProgram, HomeProgram.id == HomeProgramGrant.program_id)
                .where(
                    HomeProgram.patient_id == patient_id,
                    HomeProgramGrant.caregiver_id == caregiver_id,
                    HomeProgramGrant.revoked_at.is_(None),
                )
                .with_for_update(of=HomeProgramGrant)
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for grant in grants:
        _revoke(
            db,
            grant,
            patient_id=patient_id,
            actor=actor,
            actor_role=actor_role,
            now=now,
            clear_caregiver=True,
        )
    if grants:
        await db.flush()
    return len(grants)


async def revoke_aba_derived_grants(
    db: AsyncSession,
    *,
    patient_id: UUID,
    actor: Professional,
    actor_role: str = "coordinator",
) -> int:
    """Retirada de consentimento invalida derivação ABA dos programas de casa.

    Revoga os grants ativos de programas deste paciente que tenham ao menos uma
    tarefa vinculada a programa ABA. Grants comuns (meta comum, sem flag)
    permanecem. A concessão posterior nunca reativa o link revogado.
    """
    aba_program_ids = select(HomeProgramTask.program_id).where(
        HomeProgramTask.intervention_program_id.is_not(None)
    )
    grants = (
        (
            await db.execute(
                select(HomeProgramGrant)
                .join(HomeProgram, HomeProgram.id == HomeProgramGrant.program_id)
                .where(
                    HomeProgram.patient_id == patient_id,
                    HomeProgramGrant.program_id.in_(aba_program_ids),
                    HomeProgramGrant.revoked_at.is_(None),
                )
                .with_for_update(of=HomeProgramGrant)
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for grant in grants:
        _revoke(
            db,
            grant,
            patient_id=patient_id,
            actor=actor,
            actor_role=actor_role,
            now=now,
        )
    if grants:
        await db.flush()
    return len(grants)


HOME_PROGRAM_LINK_GONE_MESSAGE = (
    "Link do programa de casa inválido ou indisponível. "
    "Peça um novo link à profissional."
)
CHECK_IN_NOT_FOUND_MESSAGE = "Resposta do programa de casa não encontrada."
PHOTO_NOT_FOUND_MESSAGE = "Foto do programa de casa não encontrada."
MATERIAL_NOT_FOUND_MESSAGE = "Material do programa de casa não encontrado."
MATERIAL_UNAVAILABLE_MESSAGE = (
    "Este material não está disponível para a família no momento."
)


def _gone() -> HTTPException:
    """410 genérico: nunca enumera paciente, motivo financeiro ou estado interno."""
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=HOME_PROGRAM_LINK_GONE_MESSAGE,
    )


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    )


@dataclass(frozen=True)
class PublicHomeProgramContext:
    """Grant do header público, revalidado (estado, dono, responsável e ABA)."""

    grant: HomeProgramGrant
    program: HomeProgram
    patient: Patient
    owner: Professional
    caregiver: Caregiver


async def resolve_public_grant(
    db: AsyncSession, raw_token: str | None, *, lock: bool = False
) -> PublicHomeProgramContext:
    """Resolve o header ``X-Home-Program-Token`` para o programa da família.

    Qualquer estado inválido — token ausente/desconhecido/revogado/expirado,
    programa fora de ``active``, responsável removido, dono (ou prescritor)
    desativado, ou derivação ABA invalidada — vira 410 com corpo genérico, sem
    enumerar o paciente e sem expor o motivo.

    Com ``lock=True`` (mutações) adquire os locks na ordem documentada no §3.5
    (paciente → programa → grant) e revalida o estado DEPOIS do lock, para que
    revogação/retirada de consentimento no intervalo impeça o commit.
    """
    raw = (raw_token or "").strip()
    if not raw or len(raw) > 128:
        raise _gone()
    grant = await db.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.token_hash == hash_token(raw))
    )
    if grant is None:
        raise _gone()
    program = await db.get(HomeProgram, grant.program_id)
    if program is None:
        raise _gone()

    if lock:
        await db.execute(
            select(Patient.id)
            .where(Patient.id == program.patient_id)
            .with_for_update()
        )
        await db.execute(
            select(HomeProgram.id)
            .where(HomeProgram.id == program.id)
            .with_for_update()
        )
        await db.execute(
            select(HomeProgramGrant.id)
            .where(HomeProgramGrant.id == grant.id)
            .with_for_update()
        )
        await db.refresh(program)
        await db.refresh(grant)

    patient = await db.get(Patient, program.patient_id)
    owner = (
        await db.get(Professional, patient.professional_id) if patient else None
    )
    creator = await db.get(Professional, program.created_by_professional_id)
    if (
        patient is None
        or owner is None
        or owner.is_disabled
        or creator is None
        or creator.is_disabled
    ):
        raise _gone()
    if program.status != "active":
        raise _gone()
    if grant.revoked_at is not None:
        raise _gone()
    if _as_utc(grant.expires_at) <= utcnow():
        raise _gone()
    if grant.caregiver_id is None:
        raise _gone()
    caregiver = await db.get(Caregiver, grant.caregiver_id)
    if caregiver is None or caregiver.patient_id != program.patient_id:
        raise _gone()
    if await program_has_aba_tasks(db, program.id):
        try:
            await aba_gate(db, patient, owner)
        except HTTPException as exc:
            raise _gone() from exc
    return PublicHomeProgramContext(
        grant=grant,
        program=program,
        patient=patient,
        owner=owner,
        caregiver=caregiver,
    )


# --------------------------------------------------------------------------- #
# Escopo de objetos (Tarefa 5.3): tarefa, check-in, foto e material do programa
# --------------------------------------------------------------------------- #


async def require_task(
    db: AsyncSession, program_id: UUID, task_id: UUID, *, lock: bool = False
) -> HomeProgramTask:
    """Tarefa DENTRO do programa; qualquer outro caso vira 404 neutro."""
    query = select(HomeProgramTask).where(
        HomeProgramTask.id == task_id,
        HomeProgramTask.program_id == program_id,
    )
    if lock:
        query = query.with_for_update()
    task = await db.scalar(query)
    if task is None:
        raise _not_found(MATERIAL_NOT_FOUND_MESSAGE)
    return task


async def require_check_in(
    db: AsyncSession, program_id: UUID, check_in_id: UUID, *, lock: bool = False
) -> HomeProgramCheckIn:
    """Resposta DENTRO do programa; fora do escopo/inexistente vira 404."""
    query = select(HomeProgramCheckIn).where(
        HomeProgramCheckIn.id == check_in_id,
        HomeProgramCheckIn.program_id == program_id,
    )
    if lock:
        query = query.with_for_update()
    check_in = await db.scalar(query)
    if check_in is None:
        raise _not_found(CHECK_IN_NOT_FOUND_MESSAGE)
    return check_in


async def current_photo(
    db: AsyncSession, check_in_id: UUID, *, lock: bool = False
) -> HomeProgramPhoto | None:
    """Foto vigente da resposta (uma por check-in; a nova substitui a anterior)."""
    query = select(HomeProgramPhoto).where(
        HomeProgramPhoto.check_in_id == check_in_id,
        HomeProgramPhoto.status != "deleted",
    )
    if lock:
        query = query.with_for_update()
    return await db.scalar(query)


async def require_current_photo(
    db: AsyncSession, program_id: UUID, check_in_id: UUID
) -> HomeProgramPhoto:
    check_in = await require_check_in(db, program_id, check_in_id)
    photo = await current_photo(db, check_in.id)
    if photo is None:
        raise _not_found(PHOTO_NOT_FOUND_MESSAGE)
    return photo


async def require_public_material(
    db: AsyncSession,
    context: PublicHomeProgramContext,
    task_id: UUID,
    resource_id: UUID,
) -> Resource:
    """Material entregue à família: vínculo exato + versão congelada + licença.

    Revalida a licença corrente a CADA entrega (nunca serve bytes por URL
    assinada durável): retirada de licença bloqueia o ARQUIVO sem apagar as
    instruções nem a resposta da tarefa.
    """
    await require_task(db, context.program.id, task_id)
    link = await db.scalar(
        select(HomeProgramTaskResource).where(
            HomeProgramTaskResource.task_id == task_id,
            HomeProgramTaskResource.resource_id == resource_id,
        )
    )
    if link is None:
        raise _not_found(MATERIAL_NOT_FOUND_MESSAGE)
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise _not_found(MATERIAL_NOT_FOUND_MESSAGE)
    if (
        link.resource_sha256
        and resource.content_sha256
        and link.resource_sha256 != resource.content_sha256
    ):
        # Versão fixada na prescrição difere do arquivo atual: não serve bytes
        # de um material que mudou depois de vinculado.
        raise _conflict(MATERIAL_UNAVAILABLE_MESSAGE)
    licenses = await current_licenses_by_resource(db, [resource.id])
    try:
        assert_can_deliver_to_family(resource, licenses.get(resource.id))
    except ResourceLicensePolicyError as exc:
        raise _conflict(MATERIAL_UNAVAILABLE_MESSAGE) from exc
    return resource
