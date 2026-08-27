from datetime import date, time
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.schedule_block import ScheduleBlock
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


async def ensure_appointment_slot_available(
    db: AsyncSession,
    professional_id: UUID,
    appointment_date: date,
    appointment_time: time,
    duration: int,
    exclude_appointment_id: UUID | None = None,
) -> None:
    appointment_start = _time_to_minutes(appointment_time)
    appointment_end = appointment_start + duration

    appointments_result = await db.execute(
        select(Appointment).where(
            Appointment.professional_id == professional_id,
            Appointment.date == appointment_date,
            Appointment.status.notin_(["cancelado"]),
        )
    )
    for existing in appointments_result.scalars().all():
        if exclude_appointment_id and existing.id == exclude_appointment_id:
            continue
        if _time_ranges_overlap(
            appointment_start,
            appointment_end,
            _time_to_minutes(existing.time),
            _time_to_minutes(existing.time) + existing.duration,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflito de horário",
            )

    blocks_result = await db.execute(
        select(ScheduleBlock).where(
            ScheduleBlock.professional_id == professional_id,
            ScheduleBlock.start_date <= appointment_date,
            ScheduleBlock.end_date >= appointment_date,
        )
    )
    for block in blocks_result.scalars().all():
        if block.start_time is None or _time_ranges_overlap(
            appointment_start,
            appointment_end,
            _time_to_minutes(block.start_time),
            _time_to_minutes(block.end_time),
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Horário indisponível na agenda",
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
