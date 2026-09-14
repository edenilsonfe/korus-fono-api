"""Edit future occurrences atomically, retaining clinical and financial history."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.finance import PackageUsage, ReceivableItem
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.appointment import AppointmentSeriesUpdate, AppointmentSeriesUpdateResponse
from app.services.appointment_series_slots import (
    WeekdaySlotRule,
    end_time_from_duration,
    iter_recurring_child_slots,
)
from app.services.care_team_service import require_clinical_access
from app.services.google_calendar_service import queue_appointment_sync
from app.services.patient_appointment_service import appointment_occurs_in_future
from app.services.schedule_block_service import ensure_appointment_slot_available
from app.services.whatsapp_appointment_outbox import create_appointment_event_logs


async def update_appointment_series(
    db: AsyncSession,
    professional: Professional,
    appointment_id: UUID,
    body: AppointmentSeriesUpdate,
):
    await db.execute(
        select(Professional.id).where(Professional.id == professional.id).with_for_update()
    )
    selected = await db.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.professional_id == professional.id,
        )
    )
    if selected is None:
        raise HTTPException(404, "Agendamento não encontrado")
    access = await require_clinical_access(db, selected.patient_id, professional, "clinical:write")
    now = datetime.now(ZoneInfo(get_settings().clinic_timezone))
    if body.from_date < now.date():
        raise HTTPException(400, "Escolha hoje ou uma data futura para alterar a recorrência")
    root_id = selected.series_id or selected.id
    appointments = list((await db.scalars(
        select(Appointment).where(
            Appointment.professional_id == professional.id,
            Appointment.patient_id == selected.patient_id,
            or_(Appointment.id == root_id, Appointment.series_id == root_id),
        ).order_by(Appointment.date, Appointment.time).with_for_update()
    )).all())
    ids = {item.id for item in appointments}
    linked_ids = set()
    for model in (Session, ReceivableItem, PackageUsage):
        linked_ids.update((await db.scalars(
            select(model.appointment_id).where(model.appointment_id.in_(ids))
        )).all())
    mutable = [
        item for item in appointments
        if item.date >= body.from_date
        and appointment_occurs_in_future(item, now)
        and item.status in {"pendente", "confirmado"}
        and item.id not in linked_ids
    ]
    mutable_ids = {item.id for item in mutable}
    protected_slots = {
        (item.date, item.time) for item in appointments if item.id not in mutable_ids
    }
    rules = [
        WeekdaySlotRule(slot.weekday, slot.time, slot.duration)
        for slot in body.weekday_slots or []
    ] if body.frequency == "personalizado" else None
    weekdays = [rule.weekday for rule in rules] if rules else None
    slots = list(iter_recurring_child_slots(
        body.frequency,
        body.from_date,
        body.end_date,
        body.time,
        end_time_from_duration(body.time, body.duration),
        weekdays,
        duration=body.duration,
        weekday_rules=rules,
        include_first=True,
    ))
    if not slots:
        raise HTTPException(400, "Não há atendimentos nos dias selecionados nesse período")
    # Validate every target before changing rows. Only this series' editable IDs are excluded.
    targets = []
    for slot in slots:
        key = (slot.start_date, slot.start_time)
        if key in protected_slots:
            continue
        if datetime.combine(slot.start_date, slot.start_time).replace(tzinfo=now.tzinfo) <= now:
            raise HTTPException(400, "A nova rotina não pode criar atendimentos no passado")
        try:
            await ensure_appointment_slot_available(
                db, professional.id, slot.start_date, slot.start_time, slot.duration,
                exclude_appointment_ids=mutable_ids,
            )
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            raise HTTPException(
                409,
                f"{exc.detail} em {slot.start_date:%d/%m/%Y} às {slot.start_time:%H:%M}. "
                "Nenhum horário foi alterado.",
            ) from exc
        targets.append(slot)

    payload = [
        slot.model_dump(mode="json", by_alias=False) for slot in body.weekday_slots or []
    ] if rules else None
    metadata = dict(
        appointment_type="recorrente", frequency=body.frequency,
        end_date=body.end_date, weekdays=weekdays, weekday_slots=payload,
    )
    remaining = list(mutable)
    rescheduled = []
    changed = []
    created_count = updated_count = 0
    # Keep IDs and confirmations for unchanged slots, even when adding another weekday.
    exact = {(item.date, item.time, item.duration): item for item in mutable}
    unmatched = []
    for slot in targets:
        item = exact.get((slot.start_date, slot.start_time, slot.duration))
        if item:
            remaining.remove(item)
            for field, value in metadata.items():
                setattr(item, field, value)
        else:
            unmatched.append(slot)
    for slot in unmatched:
        item = next((row for row in remaining if row.date == slot.start_date), None)
        if item is None:
            # ponytail: match within a calendar week; cross-week replacements cancel/create.
            item = next((
                row for row in remaining
                if row.date.isocalendar()[:2] == slot.start_date.isocalendar()[:2]
            ), None)
        if item:
            remaining.remove(item)
            if item.date != slot.start_date or item.time != slot.start_time:
                rescheduled.append(item)
            item.date, item.time, item.duration = slot.start_date, slot.start_time, slot.duration
            updated_count += 1
        else:
            item = Appointment(
                professional_id=professional.id,
                patient_id=selected.patient_id,
                series_id=root_id,
                date=slot.start_date,
                time=slot.start_time,
                duration=slot.duration,
                status="pendente",
                type=selected.type,
                service_id=selected.service_id,
                service_name_snapshot=selected.service_name_snapshot,
                service_price_cents=selected.service_price_cents,
            )
            db.add(item)
            created_count += 1
        for field, value in metadata.items():
            setattr(item, field, value)
        changed.append(item)
    for item in remaining:
        item.status = "cancelado"
        changed.append(item)
    await db.flush()
    event_logs = await create_appointment_event_logs(db, rescheduled, "rescheduled")
    event_logs += await create_appointment_event_logs(db, remaining, "cancelled")
    google_records = []
    for item in changed:
        record = await queue_appointment_sync(db, item, access.patient.name)
        if record:
            google_records.append(record)
    return AppointmentSeriesUpdateResponse(
        created_count=created_count,
        updated_count=updated_count,
        cancelled_count=len(remaining),
        preserved_count=len(appointments) - updated_count - len(remaining),
    ), event_logs, google_records
