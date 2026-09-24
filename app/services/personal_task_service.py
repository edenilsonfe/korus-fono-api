from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient
from app.models.personal_task import PersonalTask
from app.schemas.personal_task import PersonalTaskCreate, PersonalTaskResponse, PersonalTaskUpdate


async def _patient_name(db: AsyncSession, patient_id: UUID | None, professional_id: UUID) -> str | None:
    if patient_id is None:
        return None
    name = await db.scalar(
        select(Patient.name).where(Patient.id == patient_id, Patient.professional_id == professional_id)
    )
    if name is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return name


def _response(task: PersonalTask, patient_name: str | None) -> PersonalTaskResponse:
    return PersonalTaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        due_date=task.due_date,
        patient_id=task.patient_id,
        patient_name=patient_name,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def list_personal_tasks(db: AsyncSession, professional_id: UUID) -> list[PersonalTaskResponse]:
    # ponytail: load the whole personal board; paginate if accounts accumulate thousands of tasks.
    rows = (
        await db.execute(
            select(PersonalTask, Patient.name)
            .outerjoin(Patient, Patient.id == PersonalTask.patient_id)
            .where(PersonalTask.professional_id == professional_id)
            .order_by(PersonalTask.created_at.desc(), PersonalTask.id.desc())
        )
    ).all()
    return [_response(task, patient_name) for task, patient_name in rows]


async def create_personal_task(
    db: AsyncSession, professional_id: UUID, body: PersonalTaskCreate
) -> PersonalTaskResponse:
    patient_name = await _patient_name(db, body.patient_id, professional_id)
    task = PersonalTask(professional_id=professional_id, **body.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _response(task, patient_name)


async def update_personal_task(
    db: AsyncSession, professional_id: UUID, task_id: UUID, body: PersonalTaskUpdate
) -> PersonalTaskResponse:
    task = await db.scalar(
        select(PersonalTask).where(
            PersonalTask.id == task_id, PersonalTask.professional_id == professional_id
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    changes = body.model_dump(exclude_unset=True)
    patient_name = await _patient_name(db, changes.get("patient_id", task.patient_id), professional_id)
    for name, value in changes.items():
        setattr(task, name, value)
    await db.commit()
    await db.refresh(task)
    return _response(task, patient_name)


async def delete_personal_task(db: AsyncSession, professional_id: UUID, task_id: UUID) -> None:
    task = await db.scalar(
        select(PersonalTask).where(
            PersonalTask.id == task_id, PersonalTask.professional_id == professional_id
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    await db.delete(task)
    await db.commit()
