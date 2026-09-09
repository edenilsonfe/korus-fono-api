from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.intervention_program import (
    InterventionProgramCreate,
    InterventionProgramResponse,
    InterventionProgramUpdate,
    ProgramMeasurementCreate,
    ProgramMeasurementResponse,
    ProgramMeasurementReview,
)
from app.services import intervention_program_service

router = APIRouter(
    prefix="/patients/{patient_id}/intervention-programs",
    tags=["intervention-programs"],
)
VerifiedProfessional = Annotated[Professional, Depends(require_verified_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[InterventionProgramResponse])
async def list_programs(
    patient_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.list_programs(
        db, patient_id, professional
    )


@router.post(
    "",
    response_model=InterventionProgramResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_program(
    patient_id: UUID,
    body: InterventionProgramCreate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.create_program(
        db, patient_id, professional, body
    )


@router.get("/{program_id}", response_model=InterventionProgramResponse)
async def get_program(
    patient_id: UUID,
    program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.get_program(
        db, patient_id, program_id, professional
    )


@router.patch("/{program_id}", response_model=InterventionProgramResponse)
async def update_program(
    patient_id: UUID,
    program_id: UUID,
    body: InterventionProgramUpdate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.update_program(
        db, patient_id, program_id, professional, body
    )


@router.get(
    "/{program_id}/measurements",
    response_model=list[ProgramMeasurementResponse],
)
async def list_measurements(
    patient_id: UUID,
    program_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.list_measurements(
        db, patient_id, program_id, professional
    )


@router.post(
    "/{program_id}/measurements",
    response_model=ProgramMeasurementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_measurement(
    patient_id: UUID,
    program_id: UUID,
    body: ProgramMeasurementCreate,
    response: Response,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    result, existed = await intervention_program_service.create_measurement(
        db, patient_id, program_id, professional, body
    )
    if existed:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/{program_id}/measurements/{measurement_id}/review",
    response_model=ProgramMeasurementResponse,
)
async def review_measurement(
    patient_id: UUID,
    program_id: UUID,
    measurement_id: UUID,
    body: ProgramMeasurementReview,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    return await intervention_program_service.review_measurement(
        db, patient_id, program_id, measurement_id, professional, body
    )
