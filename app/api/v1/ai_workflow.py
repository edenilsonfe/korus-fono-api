"""Rotas de IA integradas ao fluxo clínico (pilotos com feature flag)."""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.v1.ai import _get_ai_patient
from app.constants.ai_flags import AI_ASSESSMENT_GOALS_FLAG, AI_EVOLUTION_DICTATION_FLAG
from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.ai import AssessmentGoalsRequest, AssessmentGoalsResponse, EvolutionDraftRequest
from app.services import ai_workflow_service
from app.services.assistant.rate_limit import enforce_assistant_rate_limit

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/evolution-draft", status_code=status.HTTP_200_OK)
async def evolution_draft(
    body: EvolutionDraftRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await ai_workflow_service.require_ai_flag(db, professional, AI_EVOLUTION_DICTATION_FLAG)
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    await _get_ai_patient(db, body.patient_id, professional)
    return await ai_workflow_service.draft_evolution(
        db,
        professional,
        patient_id=body.patient_id,
        session_id=body.session_id,
        notes=body.notes,
    )


@router.post("/assessment-goals", response_model=AssessmentGoalsResponse)
async def assessment_goals(
    body: AssessmentGoalsRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await ai_workflow_service.require_ai_flag(db, professional, AI_ASSESSMENT_GOALS_FLAG)
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    assessment = await ai_workflow_service.load_assessment(db, body.assessment_id)
    await _get_ai_patient(db, assessment.patient_id, professional)
    try:
        return await ai_workflow_service.suggest_assessment_goals(db, professional, assessment)
    except ai_workflow_service.AssessmentGoalsParseError:
        # Resposta normal (sem exceção) para o get_db confirmar o job marcado como failed.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": ai_workflow_service.ASSESSMENT_GOALS_PARSE_ERROR},
        )
