from collections.abc import Sequence
from datetime import date, time
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.schedule_block import ScheduleBlock
from app.models.professional import Professional
from app.schemas.schedule_block import ScheduleBlockCreate, ScheduleBlockResponse


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _time_ranges_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    return first_start < second_end and first_end > second_start


def _to_response(block: ScheduleBlock) -> ScheduleBlockResponse:
    return ScheduleBlockResponse(
        id=str(block.id),
        start_date=block.start_date.isoformat(),
        end_date=block.end_date.isoformat(),
        all_day=block.start_time is None,
        start_time=block.start_time.strftime("%H:%M") if block.start_time else None,
        end_time=block.end_time.strftime("%H:%M") if block.end_time else None,
        reason=block.reason,
    )


async def lock_professional_agenda(db: AsyncSession, professional_id: UUID) -> None:
    await db.execute(
        select(Professional.id).where(Professional.id == professional_id).with_for_update()
    )


class SlotConflictError(HTTPException):
    """409 raised when a slot collides; `slot_index` points into the checked batch."""

    def __init__(self, detail: str, slot_index: int = 0):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)
        self.slot_index = slot_index


async def ensure_appointment_slots_available(
    db: AsyncSession,
    professional_id: UUID,
    slots: Sequence[tuple[date, time, int]],
    *,
    exclude_appointment_ids: set[UUID] | None = None,
    lock_professional: bool = True,
    check_within_batch: bool = False,
) -> None:
    """Validate many (date, time, duration) slots with one appointments query and
    one blocks query for the whole date range, instead of two per slot.

    With `check_within_batch`, slots also may not overlap each other (used when the
    caller is about to create all of them).
    """
    # Serialize all agenda writers, including two reservations of an empty slot.
    # Callers validating several batches in one transaction may lock once up front
    # (see `lock_professional_agenda`) and pass lock_professional=False, since
    # the row lock is held until commit/rollback anyway.
    if lock_professional:
        await lock_professional_agenda(db, professional_id)
    if not slots:
        return

    first_date = min(slot[0] for slot in slots)
    last_date = max(slot[0] for slot in slots)

    appointments_result = await db.execute(
        select(Appointment).where(
            Appointment.professional_id == professional_id,
            Appointment.date >= first_date,
            Appointment.date <= last_date,
            Appointment.status.notin_(["cancelado"]),
        )
    )
    # (start, end) minute ranges per day, built from existing appointments.
    busy_by_date: dict[date, list[tuple[int, int]]] = {}
    for existing in appointments_result.scalars().all():
        if exclude_appointment_ids and existing.id in exclude_appointment_ids:
            continue
        start = _time_to_minutes(existing.time)
        busy_by_date.setdefault(existing.date, []).append((start, start + existing.duration))

    blocks_result = await db.execute(
        select(ScheduleBlock).where(
            ScheduleBlock.professional_id == professional_id,
            ScheduleBlock.start_date <= last_date,
            ScheduleBlock.end_date >= first_date,
        )
    )
    blocks = list(blocks_result.scalars().all())

    for index, (slot_date, slot_time, duration) in enumerate(slots):
        slot_start = _time_to_minutes(slot_time)
        slot_end = slot_start + duration

        for busy_start, busy_end in busy_by_date.get(slot_date, ()):
            if _time_ranges_overlap(slot_start, slot_end, busy_start, busy_end):
                raise SlotConflictError("Conflito de horário", index)

        for block in blocks:
            if not (block.start_date <= slot_date <= block.end_date):
                continue
            if block.start_time is None or _time_ranges_overlap(
                slot_start,
                slot_end,
                _time_to_minutes(block.start_time),
                _time_to_minutes(block.end_time),
            ):
                raise SlotConflictError("Horário indisponível na agenda", index)

        if check_within_batch:
            busy_by_date.setdefault(slot_date, []).append((slot_start, slot_end))


async def ensure_appointment_slot_available(
    db: AsyncSession,
    professional_id: UUID,
    appointment_date: date,
    appointment_time: time,
    duration: int,
    exclude_appointment_id: UUID | None = None,
    *,
    exclude_appointment_ids: set[UUID] | None = None,
    lock_professional: bool = True,
) -> None:
    excluded = set(exclude_appointment_ids or ())
    if exclude_appointment_id:
        excluded.add(exclude_appointment_id)
    await ensure_appointment_slots_available(
        db,
        professional_id,
        [(appointment_date, appointment_time, duration)],
        exclude_appointment_ids=excluded or None,
        lock_professional=lock_professional,
    )


async def list_schedule_blocks(
    db: AsyncSession,
    professional_id: UUID,
    from_date: date,
    to_date: date,
) -> list[ScheduleBlockResponse]:
    result = await db.execute(
        select(ScheduleBlock)
        .where(
            ScheduleBlock.professional_id == professional_id,
            ScheduleBlock.start_date <= to_date,
            ScheduleBlock.end_date >= from_date,
        )
        .order_by(ScheduleBlock.start_date.asc(), ScheduleBlock.start_time.asc())
    )
    return [_to_response(block) for block in result.scalars().all()]


async def create_schedule_block(
    db: AsyncSession,
    professional_id: UUID,
    body: ScheduleBlockCreate,
) -> ScheduleBlockResponse:
    await db.execute(select(Professional.id).where(Professional.id == professional_id).with_for_update())
    appointments_result = await db.execute(
        select(Appointment).where(
            Appointment.professional_id == professional_id,
            Appointment.date >= body.start_date,
            Appointment.date <= body.end_date,
            Appointment.status.notin_(["cancelado"]),
        )
    )
    for appointment in appointments_result.scalars().all():
        if body.all_day or _time_ranges_overlap(
            _time_to_minutes(appointment.time),
            _time_to_minutes(appointment.time) + appointment.duration,
            _time_to_minutes(body.start_time),
            _time_to_minutes(body.end_time),
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Há agendamento no período informado",
            )

    blocks_result = await db.execute(
        select(ScheduleBlock).where(
            ScheduleBlock.professional_id == professional_id,
            ScheduleBlock.start_date <= body.end_date,
            ScheduleBlock.end_date >= body.start_date,
        )
    )
    for existing in blocks_result.scalars().all():
        if body.all_day or existing.start_time is None or _time_ranges_overlap(
            _time_to_minutes(body.start_time),
            _time_to_minutes(body.end_time),
            _time_to_minutes(existing.start_time),
            _time_to_minutes(existing.end_time),
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Já existe um bloqueio nesse período",
            )

    block = ScheduleBlock(
        professional_id=professional_id,
        start_date=body.start_date,
        end_date=body.end_date,
        start_time=None if body.all_day else body.start_time,
        end_time=None if body.all_day else body.end_time,
        reason=body.reason,
    )
    db.add(block)
    await db.commit()
    await db.refresh(block)
    return _to_response(block)


async def delete_schedule_block(
    db: AsyncSession,
    professional_id: UUID,
    block_id: UUID,
) -> None:
    result = await db.execute(
        select(ScheduleBlock).where(
            ScheduleBlock.id == block_id,
            ScheduleBlock.professional_id == professional_id,
        )
    )
    block = result.scalar_one_or_none()
    if block is None:
        raise HTTPException(status_code=404, detail="Bloqueio de agenda não encontrado")
    await db.delete(block)
    await db.commit()
