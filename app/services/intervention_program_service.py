from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.models.goal import Goal
from app.models.intervention_program import InterventionProgram, ProgramMeasurement
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.intervention_program import (
    InterventionProgramCreate,
    InterventionProgramResponse,
    InterventionProgramUpdate,
    ProgramMeasurementCreate,
    ProgramMeasurementResponse,
    ProgramMeasurementReview,
)
from app.services.care_team_service import record_access_event, require_access
from app.services.patient_access import has_permission

TRANSITIONS = {
    "draft": frozenset({"active", "archived"}),
    "active": frozenset({"paused", "mastered", "archived"}),
    "paused": frozenset({"active", "archived"}),
    "mastered": frozenset({"archived"}),
    "archived": frozenset(),
}
DEFINITION_FIELDS = frozenset(
    {
        "goal_id",
        "title",
        "operational_definition",
        "teaching_strategy",
        "mastery_percent",
        "mastery_consecutive_sessions",
        "generalization_criterion",
        "maintenance_criterion",
    }
)


def _not_found(resource: str = "Programa") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} não encontrado",
    )


async def _program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    *,
    lock: bool = False,
) -> InterventionProgram:
    query = select(InterventionProgram).where(
        InterventionProgram.id == program_id,
        InterventionProgram.patient_id == patient_id,
    )
    if lock:
        query = query.with_for_update()
    program = await db.scalar(query)
    if program is None:
        raise _not_found()
    return program


async def _validate_goal(db: AsyncSession, patient_id: UUID, goal_id: UUID | None):
    if goal_id is None:
        return
    if not await db.scalar(
        select(Goal.id).where(Goal.id == goal_id, Goal.patient_id == patient_id)
    ):
        raise _not_found("Meta")


async def mastery_eligible(db: AsyncSession, program: InterventionProgram) -> bool:
    measurements = (
        (
            await db.execute(
                select(ProgramMeasurement)
                .where(
                    ProgramMeasurement.program_id == program.id,
                    ProgramMeasurement.review_status == "approved",
                    ProgramMeasurement.participation_status == "participated",
                )
                .order_by(
                    ProgramMeasurement.recorded_at.desc(),
                    ProgramMeasurement.id.desc(),
                )
                .limit(program.mastery_consecutive_sessions)
            )
        )
        .scalars()
        .all()
    )
    return len(measurements) == program.mastery_consecutive_sessions and all(
        item.independent * 100 >= item.opportunities * program.mastery_percent
        for item in measurements
    )


async def program_response(
    db: AsyncSession, program: InterventionProgram
) -> InterventionProgramResponse:
    return InterventionProgramResponse(
        id=str(program.id),
        patient_id=str(program.patient_id),
        created_by_professional_id=str(program.created_by_professional_id),
        goal_id=str(program.goal_id) if program.goal_id else None,
        approach="aba",
        title=program.title,
        operational_definition=program.operational_definition,
        teaching_strategy=program.teaching_strategy,
        mastery_percent=program.mastery_percent,
        mastery_consecutive_sessions=program.mastery_consecutive_sessions,
        generalization_criterion=program.generalization_criterion,
        maintenance_criterion=program.maintenance_criterion,
        status=program.status,
        activated_at=program.activated_at,
        closed_at=program.closed_at,
        closed_by_professional_id=(
            str(program.closed_by_professional_id)
            if program.closed_by_professional_id
            else None
        ),
        replaces_program_id=(
            str(program.replaces_program_id) if program.replaces_program_id else None
        ),
        mastery_eligible=await mastery_eligible(db, program),
        created_at=program.created_at,
        updated_at=program.updated_at,
    )


def measurement_response(
    measurement: ProgramMeasurement,
) -> ProgramMeasurementResponse:
    return ProgramMeasurementResponse(
        id=str(measurement.id),
        client_record_id=str(measurement.client_record_id),
        program_id=str(measurement.program_id),
        session_id=str(measurement.session_id),
        recorded_by_professional_id=str(measurement.recorded_by_professional_id),
        recorded_at=measurement.recorded_at,
        participation_status=measurement.participation_status,
        opportunities=measurement.opportunities,
        independent=measurement.independent,
        prompted=measurement.prompted,
        incorrect=measurement.incorrect,
        no_response=measurement.no_response,
        prompt_counts=measurement.prompt_counts or {},
        notes=measurement.notes,
        review_status=measurement.review_status,
        reviewed_by_professional_id=(
            str(measurement.reviewed_by_professional_id)
            if measurement.reviewed_by_professional_id
            else None
        ),
        reviewed_at=measurement.reviewed_at,
        review_reason=measurement.review_reason,
        replaces_measurement_id=(
            str(measurement.replaces_measurement_id)
            if measurement.replaces_measurement_id
            else None
        ),
    )


async def create_program(
    db: AsyncSession,
    patient_id: UUID,
    actor: Professional,
    body: InterventionProgramCreate,
) -> InterventionProgramResponse:
    access = await require_access(db, patient_id, actor, "program:manage")
    await _validate_goal(db, patient_id, body.goal_id)
    if body.replaces_program_id is not None:
        replaced = await _program(db, patient_id, body.replaces_program_id)
        if replaced.status == "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Um programa em rascunho pode ser editado diretamente",
            )

    program = InterventionProgram(
        patient_id=patient_id,
        created_by_professional_id=actor.id,
        goal_id=body.goal_id,
        title=body.title.strip(),
        operational_definition=body.operational_definition.strip(),
        teaching_strategy=body.teaching_strategy.strip(),
        mastery_percent=body.mastery_percent,
        mastery_consecutive_sessions=body.mastery_consecutive_sessions,
        generalization_criterion=(
            body.generalization_criterion.strip()
            if body.generalization_criterion
            else None
        ),
        maintenance_criterion=(
            body.maintenance_criterion.strip() if body.maintenance_criterion else None
        ),
        replaces_program_id=body.replaces_program_id,
    )
    db.add(program)
    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="intervention_program_created",
        resource_type="intervention_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


async def list_programs(
    db: AsyncSession, patient_id: UUID, actor: Professional
) -> list[InterventionProgramResponse]:
    await require_access(db, patient_id, actor, "clinical:read")
    programs = (
        (
            await db.execute(
                select(InterventionProgram)
                .where(InterventionProgram.patient_id == patient_id)
                .order_by(
                    InterventionProgram.created_at.desc(),
                    InterventionProgram.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [await program_response(db, program) for program in programs]


async def get_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
) -> InterventionProgramResponse:
    await require_access(db, patient_id, actor, "clinical:read")
    return await program_response(db, await _program(db, patient_id, program_id))


async def update_program(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    body: InterventionProgramUpdate,
) -> InterventionProgramResponse:
    access = await require_access(db, patient_id, actor, "program:manage")
    program = await _program(db, patient_id, program_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    definition_changes = DEFINITION_FIELDS.intersection(changes)
    if definition_changes and program.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A definição ativa é imutável; crie um novo programa para alterá-la",
        )
    if definition_changes:
        required = definition_changes - {
            "goal_id",
            "generalization_criterion",
            "maintenance_criterion",
        }
        if any(changes[field] is None for field in required):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Campos obrigatórios do programa não podem ser removidos",
            )
        await _validate_goal(db, patient_id, changes.get("goal_id", program.goal_id))
        for field in definition_changes:
            value = changes[field]
            setattr(program, field, value.strip() if isinstance(value, str) else value)

    new_status = changes.get("status")
    if new_status and new_status != program.status:
        if new_status not in TRANSITIONS[program.status]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Transição de {program.status} para {new_status} não permitida",
            )
        if new_status == "mastered" and not await mastery_eligible(db, program):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="O critério de domínio ainda não foi atingido com dados aprovados",
            )
        now = utcnow()
        if new_status == "active" and program.activated_at is None:
            program.activated_at = now
        if new_status in {"mastered", "archived"}:
            program.closed_at = now
            program.closed_by_professional_id = actor.id
        program.status = new_status

    await db.flush()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="intervention_program_updated",
        resource_type="intervention_program",
        resource_id=program.id,
    )
    await db.flush()
    return await program_response(db, program)


def _same_measurement(
    measurement: ProgramMeasurement,
    program_id: UUID,
    body: ProgramMeasurementCreate,
) -> bool:
    return (
        measurement.program_id == program_id
        and measurement.session_id == body.session_id
        and measurement.participation_status == body.participation_status
        and measurement.opportunities == body.opportunities
        and measurement.independent == body.independent
        and measurement.prompted == body.prompted
        and measurement.incorrect == body.incorrect
        and measurement.no_response == body.no_response
        and (measurement.prompt_counts or {}) == body.prompt_counts
        and measurement.notes == (body.notes.strip() if body.notes else None)
        and measurement.replaces_measurement_id == body.replaces_measurement_id
    )


async def _idempotent_measurement(
    db: AsyncSession,
    actor: Professional,
    program_id: UUID,
    body: ProgramMeasurementCreate,
) -> ProgramMeasurement | None:
    measurement = await db.scalar(
        select(ProgramMeasurement).where(
            ProgramMeasurement.recorded_by_professional_id == actor.id,
            ProgramMeasurement.client_record_id == body.client_record_id,
        )
    )
    if measurement is not None and not _same_measurement(measurement, program_id, body):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="clientRecordId já utilizado com dados diferentes",
        )
    return measurement


async def create_measurement(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
    body: ProgramMeasurementCreate,
) -> tuple[ProgramMeasurementResponse, bool]:
    access = await require_access(db, patient_id, actor, "program:collect")
    program = await _program(db, patient_id, program_id)
    existing = await _idempotent_measurement(db, actor, program_id, body)
    if existing is not None:
        return measurement_response(existing), True
    if program.status != "active" and body.replaces_measurement_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Mensurações só podem ser registradas em programas ativos",
        )
    session = await db.scalar(
        select(Session.id).where(
            Session.id == body.session_id,
            Session.patient_id == patient_id,
        )
    )
    if session is None:
        raise _not_found("Sessão")
    if body.replaces_measurement_id is not None:
        replaced = await db.scalar(
            select(ProgramMeasurement).where(
                ProgramMeasurement.id == body.replaces_measurement_id,
                ProgramMeasurement.program_id == program_id,
                ProgramMeasurement.review_status == "voided",
            )
        )
        if replaced is None:
            raise _not_found("Mensuração substituída")
        if replaced.session_id != body.session_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A correção deve permanecer vinculada à sessão original",
            )

    can_review = has_permission(access, "measurement:review")
    now = utcnow()
    measurement = ProgramMeasurement(
        client_record_id=body.client_record_id,
        program_id=program_id,
        session_id=body.session_id,
        recorded_by_professional_id=actor.id,
        recorded_at=now,
        participation_status=body.participation_status,
        opportunities=body.opportunities,
        independent=body.independent,
        prompted=body.prompted,
        incorrect=body.incorrect,
        no_response=body.no_response,
        prompt_counts=body.prompt_counts,
        notes=body.notes.strip() if body.notes else None,
        review_status="approved" if can_review else "submitted",
        reviewed_by_professional_id=actor.id if can_review else None,
        reviewed_at=now if can_review else None,
        replaces_measurement_id=body.replaces_measurement_id,
    )
    try:
        async with db.begin_nested():
            db.add(measurement)
            await db.flush()
    except IntegrityError:
        raced = await _idempotent_measurement(db, actor, program_id, body)
        if raced is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A mensuração substituída já possui uma correção"
                    if body.replaces_measurement_id
                    else "Já existe uma mensuração vigente para esta sessão"
                ),
            )
        return measurement_response(raced), True

    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action="program_measurement_recorded",
        resource_type="program_measurement",
        resource_id=measurement.id,
    )
    await db.flush()
    return measurement_response(measurement), False


async def list_measurements(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    actor: Professional,
) -> list[ProgramMeasurementResponse]:
    await require_access(db, patient_id, actor, "clinical:read")
    await _program(db, patient_id, program_id)
    measurements = (
        (
            await db.execute(
                select(ProgramMeasurement)
                .where(ProgramMeasurement.program_id == program_id)
                .order_by(
                    ProgramMeasurement.recorded_at.desc(),
                    ProgramMeasurement.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [measurement_response(item) for item in measurements]


async def review_measurement(
    db: AsyncSession,
    patient_id: UUID,
    program_id: UUID,
    measurement_id: UUID,
    actor: Professional,
    body: ProgramMeasurementReview,
) -> ProgramMeasurementResponse:
    access = await require_access(db, patient_id, actor, "measurement:review")
    await _program(db, patient_id, program_id)
    measurement = await db.scalar(
        select(ProgramMeasurement)
        .where(
            ProgramMeasurement.id == measurement_id,
            ProgramMeasurement.program_id == program_id,
        )
        .with_for_update()
    )
    if measurement is None:
        raise _not_found("Mensuração")
    if body.action == "approve":
        if measurement.review_status == "approved":
            return measurement_response(measurement)
        if measurement.review_status == "voided":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Uma mensuração anulada não pode ser aprovada",
            )
        measurement.review_status = "approved"
        measurement.review_reason = None
    else:
        reason = body.reason.strip()
        if measurement.review_status == "voided":
            if measurement.review_reason == reason:
                return measurement_response(measurement)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A mensuração já foi anulada",
            )
        measurement.review_status = "voided"
        measurement.review_reason = reason
    measurement.reviewed_by_professional_id = actor.id
    measurement.reviewed_at = utcnow()
    record_access_event(
        db,
        patient_id=patient_id,
        actor=actor,
        actor_role=access.role,
        action=f"program_measurement_{measurement.review_status}",
        resource_type="program_measurement",
        resource_id=measurement.id,
    )
    await db.flush()
    return measurement_response(measurement)
