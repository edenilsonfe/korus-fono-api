"""IA no fluxo clínico: rascunho de evolução e metas a partir da avaliação."""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.models.professional import Professional
from app.models.session import Session as ClinicalSession
from app.services.ai_context import build_context
from app.services.ai_prompts import AI_TOOL_SPECS, build_tool_prompt
from app.services.ai_service import create_ai_job, run_llm
from app.services.feature_flag_service import FeatureFlagService

FLAG_DISABLED_DETAIL = "Recurso ainda não liberado para sua conta."


async def require_ai_flag(db: AsyncSession, professional: Professional, key: str) -> None:
    if not await FeatureFlagService(db).is_enabled(professional, key):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FLAG_DISABLED_DETAIL)


async def draft_evolution(
    db: AsyncSession,
    professional: Professional,
    *,
    patient_id: UUID,
    session_id: UUID | None,
    notes: str,
) -> dict:
    if session_id is not None:
        clinical_session = await db.get(ClinicalSession, session_id)
        if clinical_session is None or clinical_session.patient_id != patient_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sessão não encontrada.")
    spec = AI_TOOL_SPECS["evolution-draft"]
    context = await build_context(db, patient_id, spec.sections, limits=spec.limits)
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=patient_id,
        job_type="evolution-draft",
        input_data={
            "patientId": str(patient_id),
            "sessionId": str(session_id) if session_id else None,
            "notesSha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
        },
    )
    result = await run_llm(
        build_tool_prompt(spec, context=context, input_text=notes),
        spec.system,
        output=spec.output,
    )
    job.status = "completed"
    job.result = result
    job.completed_at = utcnow()
    await db.flush()
    return {"jobId": str(job.id), "status": "completed", "result": result}
