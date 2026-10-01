"""IA no fluxo clínico: rascunho de evolução e metas a partir da avaliação."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.utils import utcnow
from app.models.assessment import Assessment
from app.models.professional import Professional
from app.models.session import Session as ClinicalSession
from app.schemas.ai import AssessmentGoalSuggestion, AssessmentGoalsResponse
from app.services.ai_context import build_context, build_focused_assessment_section
from app.services.ai_json import LLMJsonError, parse_llm_json
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


ASSESSMENT_GOALS_PARSE_ERROR = "Não foi possível interpretar as sugestões da IA. Tente novamente."
MAX_SUGGESTED_GOALS = 8


class SuggestedGoal(BaseModel):
    title: str
    area: str = ""
    rationale: str = ""

    @field_validator("title", "area", "rationale", mode="before")
    @classmethod
    def _collapse_spaces(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @model_validator(mode="after")
    def _apply_limits(self) -> "SuggestedGoal":
        self.title = self.title[:255]
        self.area = self.area[:100] or "Geral"
        self.rationale = self.rationale[:600]
        return self


class SuggestedGoals(BaseModel):
    goals: list[SuggestedGoal]

    @field_validator("goals")
    @classmethod
    def _keep_titled(cls, goals: list[SuggestedGoal]) -> list[SuggestedGoal]:
        kept = [goal for goal in goals if goal.title]
        if not kept:
            raise ValueError("nenhuma meta com título")
        return kept[:MAX_SUGGESTED_GOALS]


class AssessmentGoalsParseError(Exception):
    """O LLM não devolveu JSON utilizável após uma nova tentativa."""


async def load_assessment(db: AsyncSession, assessment_id: UUID) -> Assessment:
    result = await db.execute(
        select(Assessment)
        .where(Assessment.id == assessment_id)
        .options(selectinload(Assessment.protocol), selectinload(Assessment.battery_subforms))
    )
    assessment = result.scalar_one_or_none()
    if assessment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avaliação não encontrada.")
    return assessment


async def suggest_assessment_goals(
    db: AsyncSession, professional: Professional, assessment: Assessment
) -> AssessmentGoalsResponse:
    spec = AI_TOOL_SPECS["assessment-goals"]
    context = await build_context(db, assessment.patient_id, spec.sections, limits=spec.limits)
    prompt = build_tool_prompt(
        spec, context=context, input_text=build_focused_assessment_section(assessment)
    )
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=assessment.patient_id,
        job_type="assessment-goals",
        input_data={"assessmentId": str(assessment.id)},
    )
    try:
        parsed = parse_llm_json(await run_llm(prompt, spec.system, output="json"), SuggestedGoals)
    except LLMJsonError as first_error:
        retry_prompt = (
            f"{prompt}\n\nSua resposta anterior foi rejeitada: {first_error} "
            "Responda apenas com o JSON no formato pedido."
        )
        try:
            parsed = parse_llm_json(
                await run_llm(retry_prompt, spec.system, output="json"), SuggestedGoals
            )
        except LLMJsonError as exc:
            job.status = "failed"
            job.error = "invalid_json"
            job.completed_at = utcnow()
            await db.flush()
            raise AssessmentGoalsParseError from exc
    goals = [AssessmentGoalSuggestion(**goal.model_dump()) for goal in parsed.goals]
    job.status = "completed"
    job.result = json.dumps(
        {"goals": [goal.model_dump() for goal in goals]}, ensure_ascii=False
    )
    job.completed_at = utcnow()
    await db.flush()
    return AssessmentGoalsResponse(job_id=str(job.id), goals=goals)
