import hashlib
import json
import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.ai import AIJob
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.patient import get_patient_aggregates


logger = logging.getLogger(__name__)


def hash_input(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


async def create_ai_job(
    db: AsyncSession,
    *,
    professional_id: UUID,
    patient_id: UUID | None,
    job_type: str,
    input_data: dict,
) -> AIJob:
    job = AIJob(
        professional_id=professional_id,
        patient_id=patient_id,
        job_type=job_type,
        status="pending",
        input_hash=hash_input(input_data),
        input_data=json.dumps(input_data, default=str),
    )
    db.add(job)
    await db.flush()
    return job


async def get_job(db: AsyncSession, job_id: UUID, professional_id: UUID) -> AIJob | None:
    result = await db.execute(
        select(AIJob).where(AIJob.id == job_id, AIJob.professional_id == professional_id)
    )
    return result.scalar_one_or_none()


async def build_patient_context(db: AsyncSession, patient_id: UUID) -> str:
    from app.models.evolution import Evolution
    from app.models.goal import Goal
    from app.models.assessment import Assessment

    patient = await db.get(Patient, patient_id)
    if not patient:
        return ""
    aggregates = await get_patient_aggregates(db, patient_id)
    goals = (await db.execute(select(Goal).where(Goal.patient_id == patient_id).limit(5))).scalars().all()
    evolutions = (
        await db.execute(select(Evolution).where(Evolution.patient_id == patient_id).order_by(Evolution.date.desc()).limit(3))
    ).scalars().all()
    assessments = (
        await db.execute(select(Assessment).where(Assessment.patient_id == patient_id).order_by(Assessment.date.desc()).limit(3))
    ).scalars().all()

    from app.core.diagnosis_catalog import diagnosis_labels

    professional = await db.get(Professional, patient.professional_id)
    specialty_key = professional.specialty_key if professional else "fono"
    keys = patient.diagnosis_keys or []
    diag_text = ", ".join(diagnosis_labels(keys, specialty_key)) if keys else "Não informado"

    lines = [
        f"Paciente: {patient.name}",
        f"Diagnósticos: {diag_text}",
        f"Status: {patient.status}",
        f"Sessões: {aggregates['sessions_count']}",
        f"Metas atingidas: {aggregates['goals_achieved']}/{aggregates['total_goals']}",
    ]
    if goals:
        lines.append("Metas: " + "; ".join(f"{g.title} ({g.progress}%)" for g in goals))
    if evolutions:
        lines.append("Evoluções recentes: " + "; ".join(e.title for e in evolutions))
    if assessments:
        lines.append("Avaliações: " + "; ".join(f"{a.protocol_id} ({a.percentage}%)" for a in assessments))
    return "\n".join(lines)


async def run_llm(prompt: str, system: str = "", output: str = "plain") -> str:
    settings = get_settings()
    if not settings.opencode_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ferramentas de IA não configuradas.",
        )
    from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.opencode_api_key,
        base_url=settings.opencode_base_url,
        timeout=settings.assistant_llm_timeout_seconds,
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        response = await client.chat.completions.create(
            model=settings.opencode_model,
            messages=messages,
        )
    except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
        logger.warning(
            "AI provider temporarily unavailable: error=%s status=%s",
            type(exc).__name__,
            getattr(exc, "status_code", None),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Serviço de IA temporariamente indisponível. "
                "Tente novamente em alguns minutos."
            ),
            headers={"Retry-After": "60"},
        ) from exc
    content = response.choices[0].message.content or ""
    from app.services.assistant.format_reply import sanitize_llm_markdown, sanitize_llm_plain_text

    if output == "markdown":
        return sanitize_llm_markdown(content)
    return sanitize_llm_plain_text(content)
