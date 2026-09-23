"""Pilot gates for the clinical journey; disabling writes preserves the archive."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.professional import Professional
from app.services.feature_flag_service import FeatureFlagService

WORKFLOW_FLAGS = {
    "functional_feedback": "Retorno funcional da família nas tarefas de casa",
    "clinical_reviews": "Revisão terapêutica periódica",
    "structured_discharge": "Alta com plano de continuidade",
    "patient_intake": "Pré-atendimento digital por convite",
}


async def require_workflow_enabled(db: AsyncSession, owner_id: UUID, key: str) -> None:
    owner = await db.get(Professional, owner_id)
    if owner is None or owner.is_disabled or not await FeatureFlagService(db).is_enabled(owner, key):
        raise HTTPException(403, "Este fluxo ainda não está habilitado para esta conta.")
