"""Rich clinical context builders for AI tools."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.constants.battery import BATTERY_SUBFORM_STATUS_COMPLETED
from app.core.diagnosis_catalog import diagnosis_labels
from app.models.anamnese import AnamneseEntry
from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import ClinicalDomainSnapshot, Goal
from app.models.patient import Patient
from app.models.professional import Professional

ANAMNESIS_MAX_CHARS = 2000
EVOLUTION_CONTENT_MAX_CHARS = 1500
DEFAULT_EVOLUTIONS_LIMIT = 5
DEFAULT_ASSESSMENTS_LIMIT = 5
ATTENDANCE_WINDOW_DAYS = 90

_SECTION_BUILDERS: dict[str, str] = {}


def _register(section: str):
    def decorator(fn):
        _SECTION_BUILDERS[section] = fn.__name__
        return fn

    return decorator


def _format_age(birth_date: date, on_date: date | None = None) -> str:
    ref = on_date or date.today()
    delta = relativedelta(ref, birth_date)
    if delta.months:
        return f"{delta.years} anos e {delta.months} meses"
    return f"{delta.years} anos"


def _therapy_duration(start_date: date, on_date: date | None = None) -> str:
    ref = on_date or date.today()
    delta = relativedelta(ref, start_date)
    parts: list[str] = []
    if delta.years:
        parts.append(f"{delta.years} ano{'s' if delta.years != 1 else ''}")
    if delta.months:
        parts.append(f"{delta.months} mês{'es' if delta.months != 1 else ''}")
    if not parts:
        days = (ref - start_date).days
        return f"{days} dia{'s' if days != 1 else ''}"
    return " e ".join(parts)


def _summarize_scores(scores: dict | None) -> str:
    if not scores:
        return ""
    lines: list[str] = []
    domains = scores.get("domains") or {}
    if isinstance(domains, dict):
        for slug, domain_score in domains.items():
            if not isinstance(domain_score, dict):
                continue
            title = domain_score.get("title", slug)
            parts = [title]
            if domain_score.get("level"):
                parts.append(str(domain_score["level"]))
            if domain_score.get("percentage") is not None:
                parts.append(f"{domain_score['percentage']}%")
            if domain_score.get("standard_score") is not None:
                parts.append(f"EP {domain_score['standard_score']}")
            if domain_score.get("percentile") is not None:
                parts.append(f"P{domain_score['percentile']}")
            lines.append(" — ".join(parts))
    for key in ("categories", "processes"):
        items = scores.get(key)
        if isinstance(items, dict):
            for item_id, item in items.items():
                if isinstance(item, dict):
                    title = item.get("title", item_id)
                    pct = item.get("percentage")
                    lines.append(f"{title}: {pct}%" if pct is not None else title)
        elif isinstance(items, list):
            for item in items[:8]:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("label", "")
                    if title:
                        lines.append(title)
    interpretation = scores.get("interpretation") or scores.get("summary")
    if interpretation and not lines:
        lines.append(str(interpretation))
    return "; ".join(lines[:12])


@_register("identity")
async def build_identity_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    patient = await db.get(Patient, patient_id)
    if not patient:
        return ""
    professional = await db.get(Professional, patient.professional_id)
    specialty_key = professional.specialty_key if professional else "fono"
    keys = patient.diagnosis_keys or []
    diag_text = ", ".join(diagnosis_labels(keys, specialty_key)) if keys else "Não informado"
    lines = [
        f"Nome: {patient.name}",
        f"Idade: {_format_age(patient.birth_date)}",
        f"Diagnósticos: {diag_text}",
        f"Status: {patient.status}",
        f"Início do acompanhamento: {patient.start_date.isoformat()}",
        f"Tempo em terapia: {_therapy_duration(patient.start_date)}",
    ]
    return "\n".join(lines)


@_register("anamnesis")
async def build_anamnesis_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    result = await db.execute(
        select(AnamneseEntry)
        .where(AnamneseEntry.patient_id == patient_id)
        .order_by(AnamneseEntry.section)
    )
    entries = result.scalars().all()
    if not entries:
        return ""
    lines = [f"{e.section}: {e.value}" for e in entries if e.value.strip()]
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > ANAMNESIS_MAX_CHARS:
        text = text[:ANAMNESIS_MAX_CHARS].rsplit("\n", 1)[0]
    return text


@_register("evolutions")
async def build_evolutions_section(
    db: AsyncSession, patient_id: UUID, *, limits: dict[str, int] | None = None, **_kwargs
) -> str:
    limit = (limits or {}).get("evolutions", DEFAULT_EVOLUTIONS_LIMIT)
    result = await db.execute(
        select(Evolution)
        .where(Evolution.patient_id == patient_id)
        .order_by(Evolution.date.asc())
    )
    evolutions = result.scalars().all()
    if not evolutions:
        return ""
    selected = evolutions[-limit:] if len(evolutions) > limit else evolutions
    blocks: list[str] = []
    for evo in selected:
        content = evo.content
        if len(content) > EVOLUTION_CONTENT_MAX_CHARS:
            content = content[:EVOLUTION_CONTENT_MAX_CHARS] + "…"
        date_str = evo.date.date().isoformat() if hasattr(evo.date, "date") else str(evo.date)[:10]
        blocks.append(f"[{date_str}] {evo.title}\n{content}")
    return "\n\n".join(blocks)


@_register("goals")
async def build_goals_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    result = await db.execute(
        select(Goal).where(Goal.patient_id == patient_id).order_by(Goal.status, Goal.start_date.desc())
    )
    goals = result.scalars().all()
    if not goals:
        return ""
    by_status: dict[str, list[Goal]] = {}
    for goal in goals:
        by_status.setdefault(goal.status, []).append(goal)
    blocks: list[str] = []
    for status, status_goals in by_status.items():
        lines = [
            f"- {g.title} ({g.area}) — {g.progress}% — início {g.start_date.isoformat()}"
            for g in status_goals
        ]
        blocks.append(f"{status}:\n" + "\n".join(lines))
    return "\n\n".join(blocks)


@_register("assessments")
async def build_assessments_section(
    db: AsyncSession, patient_id: UUID, *, limits: dict[str, int] | None = None, **_kwargs
) -> str:
    limit = (limits or {}).get("assessments", DEFAULT_ASSESSMENTS_LIMIT)
    result = await db.execute(
        select(Assessment)
        .where(Assessment.patient_id == patient_id)
        .options(selectinload(Assessment.protocol))
        .order_by(Assessment.date.desc())
        .limit(limit)
    )
    assessments = result.scalars().all()
    if not assessments:
        return ""
    blocks: list[str] = []
    for assessment in reversed(assessments):
        protocol_name = (
            assessment.protocol.full_name
            if assessment.protocol
            else assessment.protocol_id
        )
        lines = [
            f"Protocolo: {protocol_name}",
            f"Data: {assessment.date.isoformat()}",
            f"Resultado: {assessment.result} ({assessment.percentage}%)",
        ]
        if assessment.interpretation:
            lines.append(f"Interpretação: {assessment.interpretation}")
        scores_summary = _summarize_scores(assessment.scores)
        if scores_summary:
            lines.append(f"Domínios: {scores_summary}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_focused_assessment_section(assessment: Assessment) -> str:
    """Bloco de contexto de uma única aplicação; exige protocol e battery_subforms carregados."""
    protocol_name = (
        assessment.protocol.full_name if assessment.protocol else assessment.protocol_id
    )
    lines = [
        f"Protocolo: {protocol_name}",
        f"Data: {assessment.date.isoformat()}",
        f"Resultado: {assessment.result} ({assessment.percentage}%)",
    ]
    if assessment.interpretation:
        lines.append(f"Interpretação: {assessment.interpretation}")
    scores_summary = _summarize_scores(assessment.scores)
    if scores_summary:
        lines.append(f"Domínios: {scores_summary}")
    completed = [
        subform
        for subform in assessment.battery_subforms
        if subform.status == BATTERY_SUBFORM_STATUS_COMPLETED
    ]
    if completed:
        lines.append("Subtestes concluídos:")
        for subform in completed:
            summary = _summarize_scores(subform.scores)
            lines.append(f"- {subform.subform_slug}: {summary}" if summary else f"- {subform.subform_slug}")
    return "### Avaliação em foco\n" + "\n".join(lines)


@_register("domain_snapshots")
async def build_domain_snapshots_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    result = await db.execute(
        select(ClinicalDomainSnapshot)
        .where(ClinicalDomainSnapshot.patient_id == patient_id)
        .order_by(ClinicalDomainSnapshot.key, ClinicalDomainSnapshot.recorded_at.asc())
    )
    snapshots = result.scalars().all()
    if not snapshots:
        return ""
    by_key: dict[str, list[ClinicalDomainSnapshot]] = {}
    for snap in snapshots:
        by_key.setdefault(snap.key, []).append(snap)
    blocks: list[str] = []
    for key, series in by_key.items():
        label = series[0].label
        first = series[0]
        last = series[-1]
        delta = last.score - first.score
        if delta > 0:
            trend = "melhora"
        elif delta < 0:
            trend = "piora"
        else:
            trend = "estável"
        blocks.append(
            f"{label} ({key}): {first.score} ({first.recorded_at.isoformat()}) → "
            f"{last.score} ({last.recorded_at.isoformat()}), delta {delta:+d}, tendência {trend}"
        )
    return "\n".join(blocks)


@_register("attendance")
async def build_attendance_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    since = date.today() - timedelta(days=ATTENDANCE_WINDOW_DAYS)
    result = await db.execute(
        select(Appointment)
        .where(Appointment.patient_id == patient_id, Appointment.date >= since)
        .order_by(Appointment.date.asc())
    )
    appointments = result.scalars().all()
    if not appointments:
        return ""
    counts = {"concluida": 0, "falta": 0, "cancelada": 0, "pendente": 0}
    for appt in appointments:
        status = appt.status.lower()
        if status in counts:
            counts[status] += 1
        elif status in ("concluído", "concluido", "realizada", "realizado"):
            counts["concluida"] += 1
        else:
            counts["pendente"] += 1
    completed = counts["concluida"]
    weeks = max(ATTENDANCE_WINDOW_DAYS / 7, 1)
    avg_weekly = round(completed / weeks, 1)
    lines = [
        f"Período: últimos {ATTENDANCE_WINDOW_DAYS} dias",
        f"Sessões concluídas: {completed}",
        f"Faltas: {counts['falta']}",
        f"Cancelamentos: {counts['cancelada']}",
        f"Pendentes: {counts['pendente']}",
        f"Frequência média semanal: {avg_weekly}",
    ]
    return "\n".join(lines)


@_register("therapy_plan")
async def build_therapy_plan_section(db: AsyncSession, patient_id: UUID, **_kwargs) -> str:
    patient = await db.get(Patient, patient_id)
    if not patient or not patient.therapy_plan_content:
        return ""
    updated = ""
    if patient.therapy_plan_updated_at:
        updated = patient.therapy_plan_updated_at.date().isoformat()
    header = f"Plano atualizado em {updated}:" if updated else "Plano atual:"
    return f"{header}\n{patient.therapy_plan_content}"


_SECTION_LABELS = {
    "identity": "Identificação",
    "anamnesis": "Anamnese",
    "evolutions": "Evoluções",
    "goals": "Metas terapêuticas",
    "assessments": "Avaliações",
    "domain_snapshots": "Domínios clínicos",
    "attendance": "Frequência e agenda",
    "therapy_plan": "Plano terapêutico atual",
}


async def build_context(
    db: AsyncSession,
    patient_id: UUID,
    sections: list[str],
    *,
    limits: dict[str, int] | None = None,
    max_chars: int | None = None,
) -> str:
    """Compose clinical context blocks in section priority order with a char budget."""
    if max_chars is None:
        max_chars = get_settings().ai_context_max_chars

    blocks: list[str] = []
    total = 0
    for section in sections:
        builder_name = _SECTION_BUILDERS.get(section)
        if not builder_name:
            continue
        builder = globals()[builder_name]
        content = await builder(db, patient_id, limits=limits)
        if not content:
            continue
        label = _SECTION_LABELS.get(section, section)
        block = f"### {label}\n{content}"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 80:
                truncated = block[:remaining].rsplit("\n", 1)[0]
                blocks.append(truncated)
            break
        blocks.append(block)
        total += len(block) + 2

    return "\n\n".join(blocks)
