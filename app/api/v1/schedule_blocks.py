from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.schedule_block import ScheduleBlockCreate, ScheduleBlockResponse
from app.services.schedule_block_service import (
    create_schedule_block,
    delete_schedule_block,
    list_schedule_blocks,
)


router = APIRouter(prefix="/schedule-blocks", tags=["schedule-blocks"])


@router.get("", response_model=list[ScheduleBlockResponse])
async def list_schedule_blocks_route(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await list_schedule_blocks(db, professional.id, from_date, to_date)


@router.post(
    "",
    response_model=ScheduleBlockResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_schedule_block_route(
    body: ScheduleBlockCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await create_schedule_block(db, professional.id, body)


@router.delete("/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_block_route(
    block_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await delete_schedule_block(db, professional.id, block_id)
