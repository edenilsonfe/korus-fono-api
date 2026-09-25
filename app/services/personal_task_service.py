import calendar
from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.app_notification import AppNotification
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.personal_task import PersonalTask
from app.models.professional import Professional
from app.schemas.personal_task import (
    PersonalTaskColumn,
    PersonalTaskColumnCreate,
    PersonalTaskColumnOrder,
    PersonalTaskCreate,
    PersonalTaskResponse,
    PersonalTaskUpdate,
)


DEFAULT_COLUMNS = (
    {"id": "todo", "title": "A fazer"},
    {"id": "doing", "title": "Em andamento"},
    {"id": "done", "title": "Concluídas"},
)


def _columns(professional: Professional) -> list[dict[str, str]]:
    return [dict(column) for column in (professional.personal_task_columns or DEFAULT_COLUMNS)]


def _status_for_column(column_key: str) -> str:
    return column_key if column_key in {"todo", "doing", "done"} else "doing"


async def list_task_columns(db: AsyncSession, professional_id: UUID) -> list[PersonalTaskColumn]:
    professional = await db.get(Professional, professional_id)
    return [PersonalTaskColumn(**column) for column in _columns(professional)]


async def create_task_column(
    db: AsyncSession, professional_id: UUID, body: PersonalTaskColumnCreate
) -> PersonalTaskColumn:
    professional = await db.scalar(
        select(Professional).where(Professional.id == professional_id).with_for_update()
    )
    columns = _columns(professional)
    if len(columns) >= 20:
        raise HTTPException(status_code=422, detail="O quadro aceita até 20 colunas")
    if any(column["title"].casefold() == body.title.casefold() for column in columns):
        raise HTTPException(status_code=422, detail="Já existe uma coluna com esse nome")
    column = {"id": uuid4().hex, "title": body.title}
    professional.personal_task_columns = [*columns, column]
    await db.commit()
    return PersonalTaskColumn(**column)


async def reorder_task_columns(
    db: AsyncSession, professional_id: UUID, body: PersonalTaskColumnOrder
) -> list[PersonalTaskColumn]:
    professional = await db.scalar(
        select(Professional).where(Professional.id == professional_id).with_for_update()
    )
    columns = _columns(professional)
    if len(body.column_ids) != len(columns) or set(body.column_ids) != {
        column["id"] for column in columns
    }:
        raise HTTPException(status_code=422, detail="Informe todas as colunas uma única vez")
    by_id = {column["id"]: column for column in columns}
    ordered = [by_id[column_id] for column_id in body.column_ids]
    professional.personal_task_columns = ordered
    await db.commit()
    return [PersonalTaskColumn(**column) for column in ordered]


async def _context(db: AsyncSession, professional_id: UUID, patient_id: UUID | None, appointment_id: UUID | None):
    appointment_date = None
    if appointment_id is not None:
        appointment = await db.scalar(select(Appointment).where(
            Appointment.id == appointment_id, Appointment.professional_id == professional_id
        ))
        if appointment is None:
            raise HTTPException(status_code=404, detail="Agendamento não encontrado")
        if patient_id is not None and patient_id != appointment.patient_id:
            raise HTTPException(status_code=422, detail="O agendamento não pertence ao paciente informado")
        patient_id = appointment.patient_id
        appointment_date = appointment.date
    patient_name = None
    if patient_id is not None:
        patient_name = await db.scalar(select(Patient.name).where(
            Patient.id == patient_id, Patient.professional_id == professional_id
        ))
        if patient_name is None:
            raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return patient_id, patient_name, appointment_date


def _validate_fields(due_date, repeat_rule, remind_before_days, checklist):
    if (repeat_rule != "none" or remind_before_days is not None) and due_date is None:
        raise HTTPException(status_code=422, detail="Informe um prazo para repetir ou avisar sobre a tarefa")
    if len({item["id"] for item in checklist}) != len(checklist):
        raise HTTPException(status_code=422, detail="Itens duplicados no checklist")


def _next_due(due: date, rule: str, today: date, repeat_day: int | None = None) -> date:
    if rule == "daily":
        return due + timedelta(days=max(1, (today - due).days + 1))
    if rule == "weekly":
        return due + timedelta(days=7 * max(1, (today - due).days // 7 + 1))
    month_index = due.year * 12 + due.month
    while True:
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        next_date = date(year, month, min(repeat_day or due.day, calendar.monthrange(year, month)[1]))
        if next_date > today:
            return next_date
        month_index += 1


def _response(task: PersonalTask, patient_name: str | None, appointment_date: date | None) -> PersonalTaskResponse:
    return PersonalTaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        due_date=task.due_date,
        patient_id=task.patient_id,
        patient_name=patient_name,
        appointment_id=task.appointment_id,
        appointment_date=appointment_date,
        repeat_rule=task.repeat_rule,
        remind_before_days=task.remind_before_days,
        checklist=task.checklist,
        status=task.status,
        column_key=task.column_key,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def list_personal_tasks(db: AsyncSession, professional_id: UUID) -> list[PersonalTaskResponse]:
    # ponytail: load the whole personal board; paginate if accounts accumulate thousands of tasks.
    rows = (
        await db.execute(
            select(PersonalTask, Patient.name, Appointment.date)
            .outerjoin(Patient, Patient.id == PersonalTask.patient_id)
            .outerjoin(Appointment, Appointment.id == PersonalTask.appointment_id)
            .where(PersonalTask.professional_id == professional_id)
            .order_by(PersonalTask.created_at.desc(), PersonalTask.id.desc())
        )
    ).all()
    return [_response(task, patient_name, appointment_date) for task, patient_name, appointment_date in rows]


async def create_personal_task(
    db: AsyncSession, professional_id: UUID, body: PersonalTaskCreate
) -> PersonalTaskResponse:
    professional = await db.get(Professional, professional_id)
    if body.column_key not in {column["id"] for column in _columns(professional)}:
        raise HTTPException(status_code=404, detail="Coluna não encontrada")
    patient_id, patient_name, appointment_date = await _context(db, professional_id, body.patient_id, body.appointment_id)
    values = body.model_dump(exclude={"checklist"})
    values["patient_id"] = patient_id
    values["checklist"] = [item.model_dump(mode="json") for item in body.checklist]
    _validate_fields(values["due_date"], values["repeat_rule"], values["remind_before_days"], values["checklist"])
    values["status"] = _status_for_column(body.column_key)
    if values["status"] == "done" and (
        values["repeat_rule"] != "none" or values["remind_before_days"] is not None
    ):
        raise HTTPException(status_code=422, detail="Crie tarefas com repetição ou aviso em uma coluna ativa")
    values["repeat_day"] = values["due_date"].day if values["repeat_rule"] == "monthly" else None
    task = PersonalTask(professional_id=professional_id, **values)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _response(task, patient_name, appointment_date)


async def _archive_reminder(db: AsyncSession, task_id: UUID, professional_id: UUID) -> None:
    await db.execute(update(AppNotification).where(
        AppNotification.kind == "personal", AppNotification.type == "task_due",
        AppNotification.recipient_professional_id == professional_id,
        AppNotification.deep_link == f"/tarefas?taskId={task_id}",
    ).values(status="archived"))


async def ensure_task_reminders(db: AsyncSession, professional_id: UUID, now: datetime) -> None:
    """Create due notices when the professional opens the inbox, including missed dates."""
    today = now.astimezone(ZoneInfo(get_settings().clinic_timezone)).date()
    tasks = (await db.scalars(select(PersonalTask).where(
        PersonalTask.professional_id == professional_id,
        PersonalTask.status != "done",
        PersonalTask.remind_before_days.is_not(None),
        PersonalTask.due_date <= today + timedelta(days=30),
    ))).all()
    for task in tasks:
        if task.due_date - timedelta(days=task.remind_before_days) > today:
            continue
        notification_id = uuid5(NAMESPACE_URL, f"korus:task-due:{task.id}:{task.due_date}:{task.remind_before_days}")
        existing = await db.get(AppNotification, notification_id)
        title = "Prazo da tarefa"
        body = f"{task.title} · prazo em {task.due_date.strftime('%d/%m/%Y')}. Abra sua tarefa para conferir."
        if existing is not None:
            if existing.title != title or existing.body != body or existing.status != "published":
                existing.title, existing.body, existing.status = title, body, "published"
                await db.flush()
            continue
        try:
            async with db.begin_nested():
                db.add(AppNotification(
                    id=notification_id, kind="personal", type="task_due",
                    title=title, body=body, deep_link=f"/tarefas?taskId={task.id}",
                    severity="warning", recipient_professional_id=professional_id,
                    status="published", publish_at=now.astimezone(UTC),
                ))
                await db.flush()
        except IntegrityError:
            if await db.get(AppNotification, notification_id) is None:
                raise


async def update_personal_task(
    db: AsyncSession, professional_id: UUID, task_id: UUID, body: PersonalTaskUpdate
) -> PersonalTaskResponse:
    task = await db.scalar(
        select(PersonalTask).where(
            PersonalTask.id == task_id, PersonalTask.professional_id == professional_id
        ).with_for_update()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    changes = body.model_dump(exclude_unset=True, exclude={"checklist"})
    if "column_key" in changes:
        professional = await db.get(Professional, professional_id)
        if changes["column_key"] not in {column["id"] for column in _columns(professional)}:
            raise HTTPException(status_code=404, detail="Coluna não encontrada")
        column_status = _status_for_column(changes["column_key"])
        if "status" in changes and changes["status"] != column_status:
            raise HTTPException(status_code=422, detail="Coluna e status incompatíveis")
        changes["status"] = column_status
    elif "status" in changes:
        changes["column_key"] = changes["status"]
    if "checklist" in body.model_fields_set:
        changes["checklist"] = [item.model_dump(mode="json") for item in body.checklist]
    appointment_id = changes.get("appointment_id", task.appointment_id)
    patient_id = changes.get("patient_id", task.patient_id)
    if "appointment_id" in changes and appointment_id is not None and "patient_id" not in changes:
        patient_id = None
    patient_id, patient_name, appointment_date = await _context(db, professional_id, patient_id, appointment_id)
    changes["patient_id"] = patient_id
    _validate_fields(
        changes.get("due_date", task.due_date), changes.get("repeat_rule", task.repeat_rule),
        changes.get("remind_before_days", task.remind_before_days), changes.get("checklist", task.checklist),
    )
    if "due_date" in changes or "repeat_rule" in changes:
        next_rule = changes.get("repeat_rule", task.repeat_rule)
        next_date = changes.get("due_date", task.due_date)
        changes["repeat_day"] = next_date.day if next_rule == "monthly" else None
    was_done = task.status == "done"
    old_due = task.due_date
    old_reminder = task.remind_before_days
    for name, value in changes.items():
        setattr(task, name, value)
    if task.status == "done" or task.due_date != old_due or task.remind_before_days != old_reminder:
        await _archive_reminder(db, task.id, professional_id)
    if not was_done and task.status == "done" and task.repeat_rule != "none":
        today = datetime.now(ZoneInfo(get_settings().clinic_timezone)).date()
        repeat_rule = task.repeat_rule
        db.add(PersonalTask(
            professional_id=professional_id, patient_id=task.patient_id,
            appointment_id=None, title=task.title, description=task.description,
            due_date=_next_due(task.due_date, repeat_rule, today, task.repeat_day),
            repeat_day=task.repeat_day,
            repeat_rule=repeat_rule, remind_before_days=task.remind_before_days,
            checklist=[{**item, "done": False} for item in task.checklist],
            column_key="todo", status="todo",
        ))
        task.repeat_rule = "none"
    await db.commit()
    await db.refresh(task)
    return _response(task, patient_name, appointment_date)


async def delete_personal_task(db: AsyncSession, professional_id: UUID, task_id: UUID) -> None:
    task = await db.scalar(
        select(PersonalTask).where(
            PersonalTask.id == task_id, PersonalTask.professional_id == professional_id
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    await _archive_reminder(db, task_id, professional_id)
    await db.delete(task)
    await db.commit()
