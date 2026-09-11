"""F17/4.2 — serviço de vínculos de recursos (domínios, metas e programas ABA).

Regras implementadas (plano §3.4):

- Domínios usam chaves de ``CLINICAL_DOMAIN_CATALOG``; PUT substitui o conjunto
  inteiro, sem repetição, idempotente por par (resource, chave).
- Vínculos de meta/programa usam FKs reais com UNIQUE por par: PUT replay e
  DELETE repetido são idempotentes (204). Vínculo não altera meta, score nem
  publicação; recomendação por domínio só lista candidatos.
- Recurso privado de colega nunca é exposto por vínculo: sem ACL a resposta
  mostra indisponibilidade sem título clínico, sem DTO e sem arquivo.
- Material arquivado ou com licença retirada/incompatível não aceita novos
  vínculos (409) — mesma lógica de ``assert_can_deliver_to_family``.
"""

import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import CLINICAL_DOMAIN_CATALOG
from app.models.goal import Goal
from app.models.intervention_program import InterventionProgram
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_link import (
    GoalResourceLink,
    ProgramResourceLink,
    ResourceDomainLink,
)
from app.schemas.resource_link import LinkedResourceResponse
from app.services.care_team_service import require_access, require_clinical_access
from app.services.resource_license_service import (
    ARCHIVED_REASON,
    EXPIRED_REASON,
    HASH_MISMATCH_REASON,
    LICENSE_STATUS_REASONS,
    NO_LICENSE_REASON,
    current_licenses_by_resource,
    get_current_license,
    license_is_expired,
    license_is_valid_for_professionals,
    license_matches_content,
)

DOMAIN_KEYS: tuple[str, ...] = tuple(domain["key"] for domain in CLINICAL_DOMAIN_CATALOG)
_DOMAIN_ORDER: dict[str, int] = {key: index for index, key in enumerate(DOMAIN_KEYS)}

NO_ACCESS_REASON = "Material de outro profissional sem acesso liberado."
NO_ACCESS_TITLE = "Material de outro profissional"


class ResourceLinkNotFoundError(Exception):
    """404 — recurso/alvo fora do escopo do ator."""

    def __init__(self, detail: str = "Recurso não encontrado") -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLinkForbiddenError(Exception):
    """403 — material pessoal de colega sem ACL."""

    def __init__(self, detail: str = "Acesso negado ao recurso") -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLinkConflictError(Exception):
    """409 — material arquivado ou licença incompatível com novos vínculos."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLinkValidationError(Exception):
    """422 — domínio fora do catálogo ou repetido."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# Erros de vínculo mapeáveis para HTTP — routers usam `except LINK_ERRORS`.
LINK_ERRORS = (
    ResourceLinkNotFoundError,
    ResourceLinkForbiddenError,
    ResourceLinkConflictError,
    ResourceLinkValidationError,
)


def link_http_error(exc: Exception) -> HTTPException:
    """Converte erros de vínculo nos HTTPException do contrato."""
    if isinstance(exc, ResourceLinkNotFoundError):
        return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=exc.detail)
    if isinstance(exc, ResourceLinkForbiddenError):
        return HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=exc.detail)
    if isinstance(exc, ResourceLinkConflictError):
        return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=exc.detail)
    if isinstance(exc, ResourceLinkValidationError):
        return HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.detail
        )
    raise exc


def validate_domain_keys(keys: list[str]) -> list[str]:
    """422 para chave fora do catálogo ou repetida; devolve na ordem canônica."""
    unknown = sorted({key for key in keys if key not in _DOMAIN_ORDER})
    if unknown:
        raise ResourceLinkValidationError(
            f"Domínio clínico inválido: {', '.join(unknown)}."
        )
    if len(set(keys)) != len(keys):
        raise ResourceLinkValidationError("Chaves de domínio repetidas não são permitidas.")
    return sorted(set(keys), key=lambda key: _DOMAIN_ORDER[key])


async def domain_keys_by_resource(
    db: AsyncSession, resource_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Chaves de domínio por recurso (uma consulta, ordem canônica)."""
    if not resource_ids:
        return {}
    rows = (
        await db.execute(
            select(ResourceDomainLink.resource_id, ResourceDomainLink.domain_key).where(
                ResourceDomainLink.resource_id.in_(resource_ids)
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for resource_id, key in rows:
        grouped.setdefault(resource_id, []).append(key)
    return {
        resource_id: sorted(keys, key=lambda key: _DOMAIN_ORDER.get(key, len(_DOMAIN_ORDER)))
        for resource_id, keys in grouped.items()
    }


async def domain_keys_for_resource(db: AsyncSession, resource_id: uuid.UUID) -> list[str]:
    return (await domain_keys_by_resource(db, [resource_id])).get(resource_id, [])


async def replace_domain_links(
    db: AsyncSession, resource: Resource, keys: list[str], actor: Professional
) -> list[str]:
    """Substitui o conjunto de domínios do recurso (idempotente por par)."""
    ordered = validate_domain_keys(keys)
    existing = (
        (
            await db.execute(
                select(ResourceDomainLink).where(ResourceDomainLink.resource_id == resource.id)
            )
        )
        .scalars()
        .all()
    )
    existing_by_key = {link.domain_key: link for link in existing}
    for key, link in existing_by_key.items():
        if key not in ordered:
            await db.delete(link)
    try:
        async with db.begin_nested():
            for key in ordered:
                if key not in existing_by_key:
                    db.add(
                        ResourceDomainLink(
                            resource_id=resource.id,
                            domain_key=key,
                            created_by_professional_id=actor.id,
                        )
                    )
            await db.flush()
    except IntegrityError:
        # Replay concorrente do mesmo par: o resultado persistido é o mesmo.
        pass
    await db.flush()
    return ordered


def link_block_reason(resource: Resource, license) -> str | None:
    """Motivo (409) para bloquear novos vínculos; ``None`` quando vinculável.

    Arquivamento e retirada/expiração/incompatibilidade de licença fecham
    caminhos novos de prescrição — nunca apagam vínculos existentes.
    """
    if resource.publication_status == "archived":
        return ARCHIVED_REASON
    if license is not None and license.status == "revoked":
        return LICENSE_STATUS_REASONS.get("revoked", NO_LICENSE_REASON)
    if license is not None and license.status == "approved":
        if license_is_expired(license):
            return EXPIRED_REASON
        if not license_matches_content(license, resource):
            return HASH_MISMATCH_REASON
    return None


async def resolve_linkable_resource(
    db: AsyncSession, actor: Professional, resource_id: uuid.UUID
) -> Resource:
    """Recurso vinculável pelo ator. 404 ausente; 403 pessoal de colega sem ACL;
    409 material arquivado/licença incompatível (global sem licença vigente)."""
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise ResourceLinkNotFoundError()
    license = await get_current_license(db, resource.id)
    if resource.owner_professional_id != actor.id and not (
        resource.publication_status == "published"
        and license_is_valid_for_professionals(license, resource)
    ):
        if resource.owner_professional_id is not None:
            raise ResourceLinkForbiddenError()
        status_reason = LICENSE_STATUS_REASONS.get(license.status) if license else None
        raise ResourceLinkConflictError(status_reason or NO_LICENSE_REASON)
    block_reason = link_block_reason(resource, license)
    if block_reason is not None:
        raise ResourceLinkConflictError(block_reason)
    return resource


def linked_resource_payload(
    resource: Resource,
    license,
    domain_keys: list[str],
    actor: Professional,
) -> LinkedResourceResponse:
    """DTO do vínculo visto pelo ator — sem ACL não expõe título nem DTO."""
    from app.services.resource_service import to_resource_response

    has_acl = resource.owner_professional_id == actor.id or (
        resource.publication_status == "published"
        and license_is_valid_for_professionals(license, resource)
    )
    if not has_acl:
        return LinkedResourceResponse(
            resource_id=str(resource.id),
            title=NO_ACCESS_TITLE,
            available=False,
            reason=NO_ACCESS_REASON,
        )
    block_reason = link_block_reason(resource, license)
    return LinkedResourceResponse(
        resource_id=str(resource.id),
        title=resource.title,
        available=block_reason is None,
        reason=block_reason,
        resource=to_resource_response(
            resource, actor.id, license=license, domain_keys=domain_keys
        ),
    )


async def _linked_payloads(
    db: AsyncSession, actor: Professional, resource_ids: list[uuid.UUID]
) -> list[LinkedResourceResponse]:
    if not resource_ids:
        return []
    resources = {
        resource.id: resource
        for resource in (
            await db.execute(select(Resource).where(Resource.id.in_(resource_ids)))
        )
        .scalars()
        .all()
    }
    licenses = await current_licenses_by_resource(db, list(resources.keys()))
    keys_by_resource = await domain_keys_by_resource(db, list(resources.keys()))
    payloads: list[LinkedResourceResponse] = []
    for resource_id in resource_ids:
        resource = resources.get(resource_id)
        if resource is None:  # FK em cascata remove o vínculo junto do recurso
            continue
        payloads.append(
            linked_resource_payload(
                resource,
                licenses.get(resource.id),
                keys_by_resource.get(resource.id, []),
                actor,
            )
        )
    return payloads


async def _goal_in_scope(db: AsyncSession, patient_id: uuid.UUID, goal_id: uuid.UUID) -> Goal:
    goal = await db.scalar(
        select(Goal).where(Goal.id == goal_id, Goal.patient_id == patient_id)
    )
    if goal is None:
        raise ResourceLinkNotFoundError("Meta não encontrada")
    return goal


async def _goal_for_manage(
    db: AsyncSession, patient_id: uuid.UUID, goal_id: uuid.UUID, actor: Professional
) -> Goal:
    """Meta: clinical:write + autor da meta para gerenciar (como o PATCH atual)."""
    await require_clinical_access(db, patient_id, actor, "clinical:write")
    goal = await db.scalar(
        select(Goal).where(
            Goal.id == goal_id,
            Goal.patient_id == patient_id,
            Goal.professional_id == actor.id,
        )
    )
    if goal is None:
        raise ResourceLinkNotFoundError("Meta não encontrada")
    return goal


async def list_goal_resources(
    db: AsyncSession, patient_id: uuid.UUID, goal_id: uuid.UUID, actor: Professional
) -> list[LinkedResourceResponse]:
    """Meta: clinical:read para ler os vínculos."""
    await require_clinical_access(db, patient_id, actor)
    await _goal_in_scope(db, patient_id, goal_id)
    rows = (
        (
            await db.execute(
                select(GoalResourceLink)
                .where(GoalResourceLink.goal_id == goal_id)
                .order_by(GoalResourceLink.created_at, GoalResourceLink.id)
            )
        )
        .scalars()
        .all()
    )
    return await _linked_payloads(db, actor, [row.resource_id for row in rows])


async def link_goal_resource(
    db: AsyncSession,
    patient_id: uuid.UUID,
    goal_id: uuid.UUID,
    resource_id: uuid.UUID,
    actor: Professional,
) -> LinkedResourceResponse:
    """PUT idempotente por par (goal, resource)."""
    goal = await _goal_for_manage(db, patient_id, goal_id, actor)
    resource = await resolve_linkable_resource(db, actor, resource_id)
    existing = await db.scalar(
        select(GoalResourceLink).where(
            GoalResourceLink.goal_id == goal.id,
            GoalResourceLink.resource_id == resource.id,
        )
    )
    if existing is None:
        try:
            async with db.begin_nested():
                db.add(
                    GoalResourceLink(
                        goal_id=goal.id,
                        resource_id=resource.id,
                        created_by_professional_id=actor.id,
                    )
                )
                await db.flush()
        except IntegrityError:
            # Replay concorrente do mesmo par de FKs: idempotente.
            pass
        await db.flush()
    license = await get_current_license(db, resource.id)
    keys = await domain_keys_for_resource(db, resource.id)
    return linked_resource_payload(resource, license, keys, actor)


async def unlink_goal_resource(
    db: AsyncSession,
    patient_id: uuid.UUID,
    goal_id: uuid.UUID,
    resource_id: uuid.UUID,
    actor: Professional,
) -> None:
    """DELETE idempotente: repetir remoção conhecida continua 204."""
    goal = await _goal_for_manage(db, patient_id, goal_id, actor)
    await db.execute(
        delete(GoalResourceLink).where(
            GoalResourceLink.goal_id == goal.id,
            GoalResourceLink.resource_id == resource_id,
        )
    )
    await db.flush()


async def _program_in_scope(
    db: AsyncSession, patient_id: uuid.UUID, program_id: uuid.UUID
) -> InterventionProgram:
    program = await db.scalar(
        select(InterventionProgram).where(
            InterventionProgram.id == program_id,
            InterventionProgram.patient_id == patient_id,
        )
    )
    if program is None:
        raise ResourceLinkNotFoundError("Programa não encontrado")
    return program


async def list_program_resources(
    db: AsyncSession, patient_id: uuid.UUID, program_id: uuid.UUID, actor: Professional
) -> list[LinkedResourceResponse]:
    """Programa: clinical:read via gate ABA existente."""
    await require_access(db, patient_id, actor, "clinical:read")
    await _program_in_scope(db, patient_id, program_id)
    rows = (
        (
            await db.execute(
                select(ProgramResourceLink)
                .where(ProgramResourceLink.program_id == program_id)
                .order_by(ProgramResourceLink.created_at, ProgramResourceLink.id)
            )
        )
        .scalars()
        .all()
    )
    return await _linked_payloads(db, actor, [row.resource_id for row in rows])


async def link_program_resource(
    db: AsyncSession,
    patient_id: uuid.UUID,
    program_id: uuid.UUID,
    resource_id: uuid.UUID,
    actor: Professional,
) -> LinkedResourceResponse:
    """PUT idempotente por par (program, resource) com gate ``program:manage``."""
    await require_access(db, patient_id, actor, "program:manage")
    program = await _program_in_scope(db, patient_id, program_id)
    resource = await resolve_linkable_resource(db, actor, resource_id)
    existing = await db.scalar(
        select(ProgramResourceLink).where(
            ProgramResourceLink.program_id == program.id,
            ProgramResourceLink.resource_id == resource.id,
        )
    )
    if existing is None:
        try:
            async with db.begin_nested():
                db.add(
                    ProgramResourceLink(
                        program_id=program.id,
                        resource_id=resource.id,
                        created_by_professional_id=actor.id,
                    )
                )
                await db.flush()
        except IntegrityError:
            pass
        await db.flush()
    license = await get_current_license(db, resource.id)
    keys = await domain_keys_for_resource(db, resource.id)
    return linked_resource_payload(resource, license, keys, actor)


async def unlink_program_resource(
    db: AsyncSession,
    patient_id: uuid.UUID,
    program_id: uuid.UUID,
    resource_id: uuid.UUID,
    actor: Professional,
) -> None:
    """DELETE idempotente: repetir remoção conhecida continua 204."""
    await require_access(db, patient_id, actor, "program:manage")
    program = await _program_in_scope(db, patient_id, program_id)
    await db.execute(
        delete(ProgramResourceLink).where(
            ProgramResourceLink.program_id == program.id,
            ProgramResourceLink.resource_id == resource_id,
        )
    )
    await db.flush()
