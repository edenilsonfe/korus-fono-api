"""F16 — programa de casa: prescrição, publicação e consulta.

Fluxo: dono cria ``draft`` (version 1) → publica ``active`` (definição imutável)
→ encerra ``archived``. Nova prescrição = novo programa. Tarefas têm datas
explícitas (sem recorrência) e vínculo XOR com meta comum ou programa ABA.

Leitura: dono sem gate ABA; acesso clínico vigente (equipe assistencial) lê em
``clinical:read``, sem contatos, token ou evidência. Publicação revalida alvos,
licenças familiares F17 e gates ABA; nada de progresso clínico é alterado aqui.
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgram,
    HomeProgramCheckIn,
    HomeProgramGrant,
    HomeProgramPhoto,
    HomeProgramTask,
    HomeProgramTaskResource,
)
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.schemas.common import PaginatedResponse
from app.schemas.home_program import (
    HomeProgramCheckInSummaryResponse,
    HomeProgramCreate,
    HomeProgramPublishRequest,
    HomeProgramResponse,
    HomeProgramTaskInput,
    HomeProgramTaskResourceResponse,
    HomeProgramTaskResponse,
    HomeProgramUpdate,
)
from app.services.care_team_service import record_access_event, require_clinical_access
from app.services.home_program_access import (
    aba_gate,
    recipient_label,
    require_current_photo,
    require_owned_patient,
    require_program,
    revoke_grants_for_program,
)
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
    current_licenses_by_resource,
)

MAX_PERIOD_DAYS = 30
DEFAULT_TIMEZONE = "America/Sao_Paulo"


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _validate_period(starts_on: date, ends_on: date) -> None:
    if ends_on < starts_on:
        raise _unprocessable("O fim do programa não pode ser antes do início")
    if (ends_on - starts_on).days > MAX_PERIOD_DAYS:
        raise _unprocessable("O período do programa deve ter no máximo 30 dias")


def _validate_task_dates(
    due_dates: list[date], starts_on: date, ends_on: date
) -> None:
    for index, due_on in enumerate(due_dates, start=1):
        if due_on < starts_on or due_on > ends_on:
            raise _unprocessable(
                f"O prazo da tarefa {index} deve estar dentro do período do programa"
            )


async def _validate_targets(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    tasks: list[HomeProgramTaskInput],
) -> None:
    needs_aba = any(task.intervention_program_id is not None for task in tasks)
    for task in tasks:
        if task.intervention_program_id is not None:
            aba_program = await db.scalar(
                select(InterventionProgram).where(
                    InterventionProgram.id == task.intervention_program_id,
                    InterventionProgram.patient_id == patient_id,
                )
            )
            if aba_program is None:
                raise _not_found("Programa ABA não encontrado")
            if aba_program.status != "active":
                raise _conflict(
                    "O programa ABA precisa estar ativo para ser vinculado"
                )
        if task.goal_id is not None:
            goal = await db.scalar(
                select(Goal.id).where(
                    Goal.id == task.goal_id, Goal.patient_id == patient_id
                )
            )
            if goal is None:
                raise _not_found("Meta não encontrada")
    if needs_aba:
        patient = await require_owned_patient(db, patient_id, actor)
        await aba_gate(db, patient, actor)


async def _resolve_family_resource(
    db: AsyncSession, actor: Professional, resource_id: UUID
) -> tuple[Resource, ResourceLicense | None]:
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise _not_found("Recurso não encontrado")
    if (
        resource.owner_professional_id not in (None, actor.id)
        and resource.publication_status != "published"
    ):
        raise _not_found("Recurso não encontrado")
    licenses = await current_licenses_by_resource(db, [resource.id])
    license = licenses.get(resource.id)
    try:
        assert_can_deliver_to_family(resource, license)
    except ResourceLicensePolicyError as exc:
        raise _conflict(exc.detail) from exc
    return resource, license


async def _resolve_resources(
    db: AsyncSession, actor: Professional, tasks: list[HomeProgramTaskInput]
) -> dict[int, list[tuple[Resource, ResourceLicense | None]]]:
    resolved: dict[int, list[tuple[Resource, ResourceLicense | None]]] = {}
    for index, task in enumerate(tasks):
        items: list[tuple[Resource, ResourceLicense | None]] = []
        for resource_id in task.resource_ids:
            items.append(await _resolve_family_resource(db, actor, resource_id))
        resolved[index] = items
    return resolved


async def _replace_tasks(
    db: AsyncSession,
    program: HomeProgram,
    tasks: list[HomeProgramTaskInput],
    resources_by_task: dict[int, list[tuple[Resource, ResourceLicense | None]]],
    *,
    replace: bool,
) -> None:
    if replace:
        task_ids = select(HomeProgramTask.id).where(
            HomeProgramTask.program_id == program.id
        )
        await db.execute(
            delete(HomeProgramTaskResource).where(
                HomeProgramTaskResource.task_id.in_(task_ids)
            )
        )
        await db.execute(
            delete(HomeProgramTask).where(HomeProgramTask.program_id == program.id)
        )
        await db.flush()
    for index, task in enumerate(tasks):
        row = HomeProgramTask(
            program_id=program.id,
            position=index,
            client_task_id=task.id or uuid4(),
            title=task.title.strip(),
            instructions=task.instructions.strip(),
            due_on=task.due_on,
            goal_id=task.goal_id,
            intervention_program_id=task.intervention_program_id,
        )
        db.add(row)
        await db.flush()
        for resource, license in resources_by_task.get(index, []):
            db.add(
                HomeProgramTaskResource(
                    task_id=row.id,
                    resource_id=resource.id,
                    title_snapshot=resource.title,
                    resource_sha256=resource.content_sha256,
                    license_id=license.id if license else None,
                    license_version=license.version if license else None,
                )
            )
    await db.flush()


async def _program_tasks(
    db: AsyncSession, program_id: UUID
) -> list[HomeProgramTask]:
    return (
        (
            await db.execute(
                select(HomeProgramTask)
                .where(HomeProgramTask.program_id == program_id)
                .order_by(HomeProgramTask.position.asc(), HomeProgramTask.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def _task_resources_response(
    db: AsyncSession, tasks: list[HomeProgramTask]
) -> dict[UUID, list[HomeProgramTaskResourceResponse]]:
    if not tasks:
        return {}
    task_ids = [task.id for task in tasks]
    links = (
        (
            await db.execute(
                select(HomeProgramTaskResource)
                .where(HomeProgramTaskResource.task_id.in_(task_ids))
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
    resources = {
        resource.id: resource
        for resource in (
            await db.execute(select(Resource).where(Resource.id.in_(resource_ids)))
        )
        .scalars()
        .all()
    }
    licenses = await current_licenses_by_resource(db, resource_ids)
    payloads: dict[UUID, list[HomeProgramTaskResourceResponse]] = {
        task_id: [] for task_id in task_ids
    }
    for link in links:
        resource = resources.get(link.resource_id)
        if resource is None:
            available, reason = False, "Material não encontrado"
        else:
            try:
                assert_can_deliver_to_family(resource, licenses.get(resource.id))
            except ResourceLicensePolicyError as exc:
                available, reason = False, exc.detail
            else:
                available, reason = True, None
        payloads[link.task_id].append(
            HomeProgramTaskResourceResponse(
                resource_id=str(link.resource_id),
                title=link.title_snapshot,
                available=available,
                reason=reason,
            )
        )
    return payloads


async def program_response(
    db: AsyncSession, program: HomeProgram
) -> HomeProgramResponse:
    tasks = await _program_tasks(db, program.id)
    resources_by_task = await _task_resources_response(db, tasks)
    return HomeProgramResponse(
        id=str(program.id),
        patient_id=str(program.patient_id),
        title=program.title,
        status=program.status,
        version=program.version,
        starts_on=program.starts_on,
        ends_on=program.ends_on,
        timezone=program.timezone,
        tasks=[
            HomeProgramTaskResponse(
                id=str(task.id),
                client_task_id=str(task.client_task_id),
                title=task.title,
                instructions=task.instructions,
                due_on=task.due_on,
                goal_id=str(task.goal_id) if task.goal_id else None,
                intervention_program_id=(
                    str(task.intervention_program_id)
                    if task.intervention_program_id
                    else None
                ),
                resources=resources_by_task.get(task.id, []),
            )
            for task in tasks
        ],
        created_at=program.created_at,
        updated_at=program.updated_at,
    )


async def create_program(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    body: HomeProgramCreate,
) -> HomeProgramResponse:
    patient = await require_owned_patient(db, patient_id, actor)
    _validate_period(body.starts_on, body.ends_on)
    _validate_task_dates(
        [task.due_on for task in body.tasks], body.starts_on, body.ends_on
    )
    await _validate_targets(db, patient_id, actor, body.tasks)
    resources_by_task = await _resolve_resources(db, actor, body.tasks)

    program = HomeProgram(
        patient_id=patient.id,
        created_by_professional_id=actor.id,
        title=body.title.strip(),
        status="draft",
        version=1,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        timezone=get_settings().clinic_timezone or DEFAULT_TIMEZONE,
    )
    db.add(program)
    await db.flush()
    await _replace_tasks(db, program, body.tasks, resources_by_task, replace=False)
    record_access_event(
        db,
        patient_id=patient.id,
        actor=actor,
        actor_role="coordinator",
        action="home_program_created",
        resource_type="home_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


async def list_programs(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[HomeProgramResponse]:
    await require_clinical_access(db, patient_id, actor, "clinical:read")
    total = await db.scalar(
        select(func.count())
        .select_from(HomeProgram)
        .where(HomeProgram.patient_id == patient_id)
    )
    programs = (
        (
            await db.execute(
                select(HomeProgram)
                .where(HomeProgram.patient_id == patient_id)
                .order_by(HomeProgram.created_at.desc(), HomeProgram.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PaginatedResponse[HomeProgramResponse](
        items=[await program_response(db, program) for program in programs],
        total=total or 0,
        page=page,
        limit=limit,
    )


async def get_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
) -> HomeProgramResponse:
    await require_clinical_access(db, patient_id, actor, "clinical:read")
    return await program_response(
        db, await require_program(db, patient_id, program_id)
    )


async def update_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    body: HomeProgramUpdate,
) -> HomeProgramResponse:
    await require_owned_patient(db, patient_id, actor)
    program = await require_program(db, patient_id, program_id, lock=True)
    if program.status != "draft":
        detail = (
            "A definição publicada é imutável; crie um novo programa para alterá-la"
            if program.status == "active"
            else "Programa arquivado não pode ser editado; crie um novo programa"
        )
        raise _conflict(detail)
    if body.expected_version != program.version:
        raise _conflict(
            "A versão informada está desatualizada; recarregue o programa"
        )

    starts_on = body.starts_on or program.starts_on
    ends_on = body.ends_on or program.ends_on
    _validate_period(starts_on, ends_on)
    resources_by_task: dict[int, list[tuple[Resource, ResourceLicense | None]]] = {}
    if body.tasks is not None:
        _validate_task_dates(
            [task.due_on for task in body.tasks], starts_on, ends_on
        )
        await _validate_targets(db, patient_id, actor, body.tasks)
        resources_by_task = await _resolve_resources(db, actor, body.tasks)
    else:
        existing = await _program_tasks(db, program.id)
        _validate_task_dates(
            [task.due_on for task in existing], starts_on, ends_on
        )

    if body.title is not None:
        program.title = body.title.strip()
    program.starts_on = starts_on
    program.ends_on = ends_on
    if body.tasks is not None:
        await _replace_tasks(
            db, program, body.tasks, resources_by_task, replace=True
        )
    program.version += 1
    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role="coordinator",
        action="home_program_updated",
        resource_type="home_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


async def _revalidate_stored_targets(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    tasks: list[HomeProgramTask],
) -> None:
    """Republicar revalida os alvos persistidos (definição congelada)."""
    needs_aba = False
    for task in tasks:
        if task.intervention_program_id is not None:
            needs_aba = True
            aba_program = await db.scalar(
                select(InterventionProgram).where(
                    InterventionProgram.id == task.intervention_program_id,
                    InterventionProgram.patient_id == patient_id,
                )
            )
            if aba_program is None:
                raise _conflict("Um programa ABA vinculado não está mais disponível")
            if aba_program.status != "active":
                raise _conflict(
                    "O programa ABA precisa estar ativo para ser vinculado"
                )
        if task.goal_id is not None:
            goal = await db.scalar(
                select(Goal.id).where(
                    Goal.id == task.goal_id, Goal.patient_id == patient_id
                )
            )
            if goal is None:
                raise _conflict("Uma meta vinculada não está mais disponível")
    if needs_aba:
        patient = await require_owned_patient(db, patient_id, actor)
        await aba_gate(db, patient, actor)

    links = (
        (
            await db.execute(
                select(HomeProgramTaskResource).where(
                    HomeProgramTaskResource.task_id.in_(
                        [task.id for task in tasks]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    for link in links:
        resource = await db.get(Resource, link.resource_id)
        if resource is None:
            raise _conflict("Um material vinculado não está mais disponível")
        licenses = await current_licenses_by_resource(db, [resource.id])
        try:
            assert_can_deliver_to_family(resource, licenses.get(resource.id))
        except ResourceLicensePolicyError as exc:
            raise _conflict(exc.detail) from exc


async def publish_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    body: HomeProgramPublishRequest,
) -> HomeProgramResponse:
    await require_owned_patient(db, patient_id, actor)
    program = await require_program(db, patient_id, program_id, lock=True)
    if body.expected_version != program.version:
        raise _conflict(
            "A versão informada está desatualizada; recarregue o programa"
        )
    if program.status == "active":
        # Idempotência: o mesmo estado não republica nem reenvia nada.
        return await program_response(db, program)
    if program.status == "archived":
        raise _conflict(
            "Programa arquivado não pode ser republicado; crie um novo programa"
        )

    tasks = await _program_tasks(db, program.id)
    if not tasks:
        raise _conflict("Adicione ao menos uma tarefa antes de publicar")
    _validate_period(program.starts_on, program.ends_on)
    _validate_task_dates(
        [task.due_on for task in tasks], program.starts_on, program.ends_on
    )
    await _revalidate_stored_targets(db, patient_id, actor, tasks)

    program.status = "active"
    program.published_at = utcnow()
    program.version += 1
    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role="coordinator",
        action="home_program_published",
        resource_type="home_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


async def archive_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
) -> HomeProgramResponse:
    await require_owned_patient(db, patient_id, actor)
    program = await require_program(db, patient_id, program_id, lock=True)
    if program.status == "archived":
        return await program_response(db, program)

    program.status = "archived"
    program.archived_at = utcnow()
    program.archived_by_professional_id = actor.id
    program.version += 1
    await revoke_grants_for_program(
        db, patient_id=patient_id, program_id=program.id, actor=actor
    )
    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role="coordinator",
        action="home_program_archived",
        resource_type="home_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


# --------------------------------------------------------------------------- #
# Acompanhamento profissional (Tarefa 5.3): check-ins autodeclarados e foto
# --------------------------------------------------------------------------- #

RESPONDER_LABEL = "Responsável"


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    )


async def list_check_ins(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    *,
    page: int,
    limit: int,
) -> PaginatedResponse[HomeProgramCheckInSummaryResponse]:
    """Respostas da família (autodeclaradas) para leitura clínica autorizada.

    Contatos nunca aparecem; o nome do responsável só é devolvido ao dono do
    paciente — os demais leitores clínicos veem o rótulo neutro "Responsável".
    """
    await require_clinical_access(db, patient_id, actor, "clinical:read")
    program = await require_program(db, patient_id, program_id)
    patient = await db.get(Patient, patient_id)
    is_owner = patient is not None and patient.professional_id == actor.id

    total = await db.scalar(
        select(func.count())
        .select_from(HomeProgramCheckIn)
        .where(HomeProgramCheckIn.program_id == program.id)
    )
    rows = (
        (
            await db.execute(
                select(HomeProgramCheckIn)
                .where(HomeProgramCheckIn.program_id == program.id)
                .order_by(
                    HomeProgramCheckIn.responded_at.desc(),
                    HomeProgramCheckIn.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return PaginatedResponse[HomeProgramCheckInSummaryResponse](
            items=[], total=total or 0, page=page, limit=limit
        )

    tasks = {
        task.id: task.title
        for task in (
            await db.execute(
                select(HomeProgramTask).where(
                    HomeProgramTask.id.in_([row.task_id for row in rows])
                )
            )
        )
        .scalars()
        .all()
    }
    grant_ids = [row.grant_id for row in rows if row.grant_id is not None]
    grants: dict[UUID, HomeProgramGrant] = {}
    if grant_ids:
        grants = {
            grant.id: grant
            for grant in (
                await db.execute(
                    select(HomeProgramGrant).where(HomeProgramGrant.id.in_(grant_ids))
                )
            )
            .scalars()
            .all()
        }
    photo_ids = {
        photo_check_in_id
        for photo_check_in_id in (
            await db.execute(
                select(HomeProgramPhoto.check_in_id).where(
                    HomeProgramPhoto.check_in_id.in_([row.id for row in rows]),
                    HomeProgramPhoto.status != "deleted",
                )
            )
        )
        .scalars()
        .all()
    }
    items = []
    for row in rows:
        grant = grants.get(row.grant_id) if row.grant_id else None
        actor_label = (
            recipient_label(grant) if is_owner and grant is not None else RESPONDER_LABEL
        )
        items.append(
            HomeProgramCheckInSummaryResponse(
                id=str(row.id),
                task_id=str(row.task_id),
                task_title=tasks.get(row.task_id, ""),
                done=row.done,
                comment=row.comment,
                responded_at=_as_utc(row.responded_at),
                version=row.version,
                has_photo=row.id in photo_ids,
                actor_label=actor_label,
            )
        )
    return PaginatedResponse[HomeProgramCheckInSummaryResponse](
        items=items, total=total or 0, page=page, limit=limit
    )


async def professional_check_in_photo(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    check_in_id: UUID,
    actor: Professional,
) -> HomeProgramPhoto:
    """Foto vigente para leitura clínica autenticada (ACL atual, sem contatos)."""
    await require_clinical_access(db, patient_id, actor, "clinical:read")
    program = await require_program(db, patient_id, program_id)
    return await require_current_photo(db, program.id, check_in_id)
