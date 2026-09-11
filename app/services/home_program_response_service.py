"""F16 — respostas públicas da família: check-ins, revisões e idempotência.

Opera sobre o grant resolvido do header ``X-Home-Program-Token`` (Tarefa 5.1):
uma resposta atual por tarefa, comandos idempotentes por ``clientRecordId``
(``HomeProgramEvent`` com ``UNIQUE(grant_id, client_record_id)``) e histórico de
edições preservado em ``HomeProgramCheckInRevision`` com o grant autor real.

Ordem de locks documentada no §3.5: paciente → programa → grant → tarefa/
check-in. A revalidação acontece DEPOIS do lock, então revogação/retirada de
consentimento no intervalo impede o commit posterior. Timestamps e autoria
nunca vêm do cliente. Nada aqui altera meta, medição ABA ou evolução.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.models.home_program import (
    HomeProgramCheckIn,
    HomeProgramCheckInRevision,
    HomeProgramEvent,
    HomeProgramPhoto,
    HomeProgramTask,
    HomeProgramTaskResource,
)
from app.models.resource import Resource
from app.schemas.home_program import (
    HomeProgramCheckInCreate,
    HomeProgramCheckInResponse,
    HomeProgramCheckInUpdate,
    PublicHomeProgramCheckInResponse,
    PublicHomeProgramMaterialResponse,
    PublicHomeProgramResponse,
    PublicHomeProgramTaskResponse,
)
from app.services.entitlement_service import EntitlementService
from app.services.home_program_access import (
    PublicHomeProgramContext,
    resolve_public_grant,
)
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
    current_licenses_by_resource,
)

CHECK_IN_CREATED_EVENT = "home_program_check_in_created"
CHECK_IN_UPDATED_EVENT = "home_program_check_in_updated"

# 403 da escrita pública: neutro de propósito (não expõe motivo financeiro).
ENTITLEMENT_BLOCKED_MESSAGE = (
    "As respostas do programa de casa estão indisponíveis no momento. "
    "Tente novamente mais tarde."
)
TASK_NOT_FOUND_MESSAGE = "Tarefa do programa de casa não encontrada."
CHECK_IN_NOT_FOUND_MESSAGE = "Resposta do programa de casa não encontrada."
TASK_ALREADY_ANSWERED_MESSAGE = (
    "Esta tarefa já foi respondida; edite a resposta existente."
)
CLIENT_RECORD_REUSED_MESSAGE = (
    "Este identificador de registro já foi usado com outro conteúdo."
)
STALE_VERSION_MESSAGE = (
    "A versão informada está desatualizada; recarregue as respostas."
)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ENTITLEMENT_BLOCKED_MESSAGE,
    )


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    )


def _first_name(name: str | None) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else ""


def _normalize_command(payload: dict) -> str:
    """SHA-256 canônico do comando (sem comentário/token em claro em eventos)."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CheckInCommandResult:
    """Resultado de um comando de check-in; ``created`` distingue 201 de 200."""

    check_in: HomeProgramCheckInResponse
    created: bool


async def _require_entitlement(
    db: AsyncSession, context: PublicHomeProgramContext
) -> None:
    """Revalida ``can_write`` do dono MESMO SEM JWT (família usa token)."""
    if not await EntitlementService(db).can_write(context.owner):
        raise _forbidden()


async def _photo_check_ins(
    db: AsyncSession, check_in_ids: list[UUID | None]
) -> set[UUID]:
    ids = [check_in_id for check_in_id in check_in_ids if check_in_id is not None]
    if not ids:
        return set()
    rows = (
        (
            await db.execute(
                select(HomeProgramPhoto.check_in_id).where(
                    HomeProgramPhoto.check_in_id.in_(ids),
                    HomeProgramPhoto.status != "deleted",
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def _check_in_response(
    row: HomeProgramCheckIn, *, has_photo: bool
) -> HomeProgramCheckInResponse:
    return HomeProgramCheckInResponse(
        id=str(row.id),
        task_id=str(row.task_id),
        done=row.done,
        comment=row.comment,
        responded_at=_as_utc(row.responded_at),
        version=row.version,
        has_photo=has_photo,
    )


def _embedded_check_in(
    row: HomeProgramCheckIn, *, has_photo: bool
) -> PublicHomeProgramCheckInResponse:
    return PublicHomeProgramCheckInResponse(
        id=str(row.id),
        done=row.done,
        comment=row.comment,
        responded_at=_as_utc(row.responded_at),
        version=row.version,
        has_photo=has_photo,
    )


async def _materials_by_task(
    db: AsyncSession, tasks: list[HomeProgramTask]
) -> dict[UUID, list[PublicHomeProgramMaterialResponse]]:
    payload: dict[UUID, list[PublicHomeProgramMaterialResponse]] = {
        task.id: [] for task in tasks
    }
    if not tasks:
        return payload
    links = (
        (
            await db.execute(
                select(HomeProgramTaskResource)
                .where(
                    HomeProgramTaskResource.task_id.in_([task.id for task in tasks])
                )
                .order_by(
                    HomeProgramTaskResource.created_at.asc(),
                    HomeProgramTaskResource.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    resource_ids = list({link.resource_id for link in links})
    licenses = await current_licenses_by_resource(db, resource_ids)
    resources: dict[UUID, Resource] = {}
    if resource_ids:
        resources = {
            resource.id: resource
            for resource in (
                await db.execute(select(Resource).where(Resource.id.in_(resource_ids)))
            )
            .scalars()
            .all()
        }
    for link in links:
        resource = resources.get(link.resource_id)
        license_row = licenses.get(link.resource_id)
        available = False
        if resource is not None:
            try:
                assert_can_deliver_to_family(resource, license_row)
            except ResourceLicensePolicyError:
                available = False
            else:
                available = True
        payload[link.task_id].append(
            PublicHomeProgramMaterialResponse(
                id=str(link.resource_id),
                title=link.title_snapshot,
                attribution=(
                    license_row.attribution
                    if license_row is not None and license_row.attribution
                    else None
                ),
                available=available,
            )
        )
    return payload


async def build_public_response(
    db: AsyncSession, context: PublicHomeProgramContext
) -> PublicHomeProgramResponse:
    """Projeção mínima da família; GET válido permanece em read-only."""
    can_respond = await EntitlementService(db).can_write(context.owner)
    tasks = (
        (
            await db.execute(
                select(HomeProgramTask)
                .where(HomeProgramTask.program_id == context.program.id)
                .order_by(HomeProgramTask.position.asc(), HomeProgramTask.id.asc())
            )
        )
        .scalars()
        .all()
    )
    check_ins: dict[UUID, HomeProgramCheckIn] = {}
    if tasks:
        rows = (
            (
                await db.execute(
                    select(HomeProgramCheckIn).where(
                        HomeProgramCheckIn.task_id.in_([task.id for task in tasks])
                    )
                )
            )
            .scalars()
            .all()
        )
        check_ins = {row.task_id: row for row in rows}
    photo_ids = await _photo_check_ins(db, [row.id for row in check_ins.values()])
    materials = await _materials_by_task(db, tasks)
    return PublicHomeProgramResponse(
        id=str(context.program.id),
        title=context.program.title,
        patient_first_name=_first_name(context.patient.name),
        professional_name=context.owner.name,
        starts_on=context.program.starts_on,
        ends_on=context.program.ends_on,
        expires_at=_as_utc(context.grant.expires_at),
        can_respond=can_respond,
        tasks=[
            PublicHomeProgramTaskResponse(
                id=str(task.id),
                title=task.title,
                instructions=task.instructions,
                due_on=task.due_on,
                materials=materials.get(task.id, []),
                check_in=(
                    _embedded_check_in(
                        check_ins[task.id],
                        has_photo=check_ins[task.id].id in photo_ids,
                    )
                    if task.id in check_ins
                    else None
                ),
            )
            for task in tasks
        ],
    )


async def _existing_event(
    db: AsyncSession, *, grant_id: UUID, client_record_id: UUID
) -> HomeProgramEvent | None:
    return await db.scalar(
        select(HomeProgramEvent).where(
            HomeProgramEvent.grant_id == grant_id,
            HomeProgramEvent.client_record_id == client_record_id,
        )
    )


async def create_check_in(
    db: AsyncSession,
    *,
    raw_token: str | None,
    task_id: UUID,
    body: HomeProgramCheckInCreate,
) -> CheckInCommandResult:
    """Cria a resposta atual da tarefa (201) ou devolve o replay idêntico (200)."""
    context = await resolve_public_grant(db, raw_token)
    await _require_entitlement(db, context)
    context = await resolve_public_grant(db, raw_token, lock=True)
    program = context.program
    grant = context.grant

    task = await db.scalar(
        select(HomeProgramTask)
        .where(
            HomeProgramTask.id == task_id,
            HomeProgramTask.program_id == program.id,
        )
        .with_for_update()
    )
    if task is None:
        raise _not_found(TASK_NOT_FOUND_MESSAGE)

    payload_hash = _normalize_command(
        {
            "operation": CHECK_IN_CREATED_EVENT,
            "taskId": str(task.id),
            "done": body.done,
            "comment": body.comment,
        }
    )
    event = await _existing_event(
        db, grant_id=grant.id, client_record_id=body.client_record_id
    )
    if event is not None:
        if event.payload_hash != payload_hash or event.check_in_id is None:
            raise _conflict(CLIENT_RECORD_REUSED_MESSAGE)
        row = await db.get(HomeProgramCheckIn, event.check_in_id)
        if row is None or row.program_id != program.id:
            raise _conflict(CLIENT_RECORD_REUSED_MESSAGE)
        photo_ids = await _photo_check_ins(db, [row.id])
        return CheckInCommandResult(
            check_in=_check_in_response(row, has_photo=row.id in photo_ids),
            created=False,
        )

    current = await db.scalar(
        select(HomeProgramCheckIn).where(HomeProgramCheckIn.task_id == task.id)
    )
    if current is not None:
        raise _conflict(TASK_ALREADY_ANSWERED_MESSAGE)

    now = utcnow()
    check_in = HomeProgramCheckIn(
        program_id=program.id,
        task_id=task.id,
        grant_id=grant.id,
        done=body.done,
        comment=body.comment,
        version=1,
        responded_at=now,
    )
    db.add(check_in)
    await db.flush()
    db.add(
        HomeProgramEvent(
            program_id=program.id,
            grant_id=grant.id,
            task_id=task.id,
            check_in_id=check_in.id,
            event_type=CHECK_IN_CREATED_EVENT,
            payload_hash=payload_hash,
            client_record_id=body.client_record_id,
            result_version=1,
            occurred_at=now,
        )
    )
    await db.flush()
    return CheckInCommandResult(
        check_in=_check_in_response(check_in, has_photo=False),
        created=True,
    )


async def update_check_in(
    db: AsyncSession,
    *,
    raw_token: str | None,
    check_in_id: UUID,
    body: HomeProgramCheckInUpdate,
) -> CheckInCommandResult:
    """Edita a resposta atual: nova versão + revisão anterior preservada."""
    context = await resolve_public_grant(db, raw_token)
    await _require_entitlement(db, context)
    context = await resolve_public_grant(db, raw_token, lock=True)
    grant = context.grant

    row = await db.scalar(
        select(HomeProgramCheckIn)
        .where(
            HomeProgramCheckIn.id == check_in_id,
            HomeProgramCheckIn.program_id == context.program.id,
        )
        .with_for_update()
    )
    if row is None:
        raise _not_found(CHECK_IN_NOT_FOUND_MESSAGE)

    payload_hash = _normalize_command(
        {
            "operation": CHECK_IN_UPDATED_EVENT,
            "checkInId": str(row.id),
            "expectedVersion": body.expected_version,
            "done": body.done,
            "comment": body.comment,
        }
    )
    event = await _existing_event(
        db, grant_id=grant.id, client_record_id=body.client_record_id
    )
    if event is not None:
        if event.payload_hash != payload_hash or event.check_in_id != row.id:
            raise _conflict(CLIENT_RECORD_REUSED_MESSAGE)
        photo_ids = await _photo_check_ins(db, [row.id])
        return CheckInCommandResult(
            check_in=_check_in_response(row, has_photo=row.id in photo_ids),
            created=False,
        )

    if body.expected_version != row.version:
        raise _conflict(STALE_VERSION_MESSAGE)

    now = utcnow()
    db.add(
        HomeProgramCheckInRevision(
            check_in_id=row.id,
            grant_id=row.grant_id,
            version=row.version,
            done=row.done,
            comment=row.comment,
            recorded_at=now,
        )
    )
    row.done = body.done
    row.comment = body.comment
    row.version += 1
    row.responded_at = now
    row.grant_id = grant.id
    db.add(
        HomeProgramEvent(
            program_id=context.program.id,
            grant_id=grant.id,
            task_id=row.task_id,
            check_in_id=row.id,
            event_type=CHECK_IN_UPDATED_EVENT,
            payload_hash=payload_hash,
            client_record_id=body.client_record_id,
            result_version=row.version,
            occurred_at=now,
        )
    )
    await db.flush()
    photo_ids = await _photo_check_ins(db, [row.id])
    return CheckInCommandResult(
        check_in=_check_in_response(row, has_photo=row.id in photo_ids),
        created=False,
    )
