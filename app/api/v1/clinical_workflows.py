from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.clinical_workflow import ClinicalWorkflowCapabilities
from app.services.feature_flag_service import FeatureFlagService
from app.services.patient_access import resolve_clinical_patient_access

router = APIRouter(tags=["clinical-workflows"])


@router.get("/patients/{patient_id}/clinical-workflows", response_model=ClinicalWorkflowCapabilities)
async def clinical_workflow_capabilities(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    access = await resolve_clinical_patient_access(db, patient_id, professional)
    if access is None or "clinical:read" not in access.permissions:
        raise HTTPException(404, "Paciente não encontrado")
    owner = await db.get(Professional, access.patient.professional_id)
    if owner is None or owner.is_disabled:
        raise HTTPException(404, "Paciente não encontrado")
    flags = FeatureFlagService(db)
    return ClinicalWorkflowCapabilities(
        functional_feedback_enabled=await flags.is_enabled(owner, "functional_feedback"),
        clinical_reviews_enabled=await flags.is_enabled(owner, "clinical_reviews"),
        structured_discharge_enabled=await flags.is_enabled(owner, "structured_discharge"),
        patient_intake_enabled=await flags.is_enabled(owner, "patient_intake"),
        can_write_review="clinical:write" in access.permissions,
        can_finalize_review="therapy_plan:write" in access.permissions,
        can_discharge=access.is_owner,
        can_manage_intake=access.is_owner,
    )
