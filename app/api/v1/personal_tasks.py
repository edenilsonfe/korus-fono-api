from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.personal_task import PersonalTaskCreate, PersonalTaskResponse, PersonalTaskUpdate
from app.services.personal_task_service import (
    create_personal_task,
    delete_personal_task,
    list_personal_tasks,
    update_personal_task,
)


router = APIRouter(prefix="/personal-tasks", tags=["personal-tasks"])


@router.get("", response_model=list[PersonalTaskResponse])
async def list_tasks(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await list_personal_tasks(db, professional.id)


@router.post("", response_model=PersonalTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: PersonalTaskCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await create_personal_task(db, professional.id, body)


@router.patch("/{task_id}", response_model=PersonalTaskResponse)
async def update_task(
    task_id: UUID,
    body: PersonalTaskUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await update_personal_task(db, professional.id, task_id, body)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await delete_personal_task(db, professional.id, task_id)
