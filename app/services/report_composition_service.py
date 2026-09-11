"""Consolidated multi-instrument report composition (F3).

- ``list_report_sources``: owner-scoped selectable sources; assessments only
  when completed; a battery is the aggregated ``Assessment`` row (one item).
- ``create_consolidated_report``: validates ownership, every selected ID,
  cardinality, distinct protocols, completed state, comparisons and the context
  budget *before* the LLM call. Captures the selection, lets the LLM draft only
  the Síntese/Conduta sections, then revalidates scope/state/hash before
  persisting. No DB lock is held while the LLM runs and there is no automatic
  LLM retry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.ai import AIReport
from app.models.assessment import (
    ASSESSMENT_STATUS_COMPLETED,
    Assessment,
    ProtocolCatalog,
)
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_composition import AIReportComposition
from app.models.session import Session
from app.schemas.ai import AIReportCreate
from app.schemas.report_composition import ReportCompositionCreate
from app.services.ai_context import _summarize_scores, build_identity_section
from app.services.ai_prompts import AI_TOOL_SPECS, build_tool_prompt
from app.services.ai_service import create_ai_job, run_llm
from app.services.assessment_comparison import (
    AssessmentComparisonResult,
    compare_assessments,
)
from app.services.timeline import create_timeline_event

CONSOLIDATED_REPORT_TYPE = "consolidado"
TEMPLATE_VERSION = "consolidado.v1"
SOURCE_KINDS = ("assessment", "evolution", "session", "goal")
SOURCE_SUMMARY_MAX_CHARS = 200

_NORM_WARNING_LEVELS = frozenset(
    {
        "partial",
        "qualitative",
        "reference",
        "public_reference",
        "structure_only",
        "stub",
        "unknown",
    }
)


@dataclass
class CompositionCapture:
    """Everything captured at generation time for one composition."""

    sources: list[dict] = field(default_factory=list)
    comparisons: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context: str = ""
    source_hashes: dict[str, str] = field(default_factory=dict)
    context_hash: str = ""
    identity: str = ""
    instruments: str = ""


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit]


def _source_key(kind: str, source_id: UUID) -> str:
    return f"{kind}:{source_id}"


def _iso(value: date | datetime) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def _number(value: float | None) -> str:
    return "n/d" if value is None else f"{value:g}"


def _norms_status(scores: dict | None) -> dict | None:
    if not isinstance(scores, dict):
        return None
    norms = scores.get("norms_status")
    return norms if isinstance(norms, dict) else None


def _norm_warning(protocol_label: str, scores: dict | None) -> str | None:
    norms = _norms_status(scores)
    if norms is None:
        return None
    level = str(norms.get("level") or "")
    label = str(norms.get("label") or "").strip()
    if not label or level not in _NORM_WARNING_LEVELS:
        return None
    detail = str(norms.get("detail") or "").strip()
    return f"{protocol_label}: {label}" + (f" — {detail}" if detail else "")


def _assessment_block(assessment: Assessment, label: str, author: str) -> str:
    lines = [
        f"### Avaliação: {label}",
        f"Protocolo: {assessment.protocol_id}",
        f"Status: {assessment.status}",
        f"Data: {assessment.date.isoformat()}",
        f"Autor: {author}",
        f"Resultado: {assessment.result or 'não informado'} ({assessment.percentage}%)",
    ]
    if assessment.interpretation:
        lines.append(f"Interpretação: {assessment.interpretation}")
    scores_summary = _summarize_scores(assessment.scores)
    if scores_summary:
        lines.append(f"Domínios: {scores_summary}")
    norms = _norms_status(assessment.scores)
    if norms is not None and str(norms.get("label") or "").strip():
        lines.append(f"Normas: {norms['label']}")
    return "\n".join(lines)


def _instrument_line(assessment: Assessment, label: str, author: str) -> str:
    return (
        f"- {label} ({assessment.protocol_id}) — {assessment.date.isoformat()} — "
        f"{assessment.result or 'não informado'} — {assessment.percentage}% — "
        f"responsável: {author}"
    )


def _evolution_block(evolution: Evolution, author: str) -> str:
    return "\n".join(
        [
            f"### Evolução: {evolution.title or 'Evolução'}",
            f"Data: {_iso(evolution.date)}",
            f"Autor: {author}",
            evolution.content,
        ]
    )


def _session_block(session: Session, author: str) -> str:
    lines = [
        f"### Sessão: {session.type or 'Sessão'}",
        f"Data: {_iso(session.date)}",
        f"Autor: {author}",
        f"Duração: {session.duration} min",
    ]
    objectives = [str(item) for item in (session.objectives or [])]
    if objectives:
        lines.append("Objetivos: " + "; ".join(objectives))
    if session.notes:
        lines.append(f"Notas: {session.notes}")
    return "\n".join(lines)


def _goal_block(goal: Goal, author: str) -> str:
    return "\n".join(
        [
            f"### Meta: {goal.title}",
            f"Área: {goal.area}",
            f"Status: {goal.status}",
            f"Progresso: {goal.progress}%",
            f"Início: {goal.start_date.isoformat()}",
            f"Autor: {author}",
        ]
    )


def _comparison_text(result: AssessmentComparisonResult) -> str:
    lines = [f"- {result.summary}"]
    changed = [metric for metric in result.metrics if metric.delta not in (None, 0)]
    if changed:
        details = "; ".join(
            f"{metric.label}: {_number(metric.base)} → {_number(metric.target)} ({metric.delta:+g})"
            for metric in changed
        )
        lines.append(f"  Métricas alteradas: {details}")
    return "\n".join(lines)


def compose_consolidated_content(*, identity: str, instruments: str, drafted: str) -> str:
    """Assemble the four fixed top-level sections of a consolidated report."""
    body = (drafted or "").strip()
    if "## Síntese" not in body and "## Conduta" not in body:
        body = f"## Síntese\n{body}"
    return "\n\n".join(
        [
            "## Identificação\n" + identity.strip(),
            "## Instrumentos\n" + instruments.strip(),
            body,
        ]
    )


# --------------------------------------------------------------------------- #
# Listing (GET /patients/{patient_id}/report-sources)
# --------------------------------------------------------------------------- #


def _assessment_summary(assessment: Assessment) -> str:
    parts: list[str] = []
    if assessment.result:
        parts.append(assessment.result)
    if assessment.percentage is not None:
        parts.append(f"{assessment.percentage}%")
    text = " — ".join(parts)
    scores_summary = _summarize_scores(assessment.scores)
    if scores_summary:
        text = f"{text} — {scores_summary}" if text else scores_summary
    return text or "Resultado não informado"


async def _list_assessments(db, *, patient_id, from_date, to_date, offset, limit):
    filters = [Assessment.patient_id == patient_id, Assessment.status == ASSESSMENT_STATUS_COMPLETED]
    if from_date is not None:
        filters.append(Assessment.date >= from_date)
    if to_date is not None:
        filters.append(Assessment.date <= to_date)
    total = int(await db.scalar(select(func.count()).select_from(Assessment).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(Assessment, ProtocolCatalog.full_name, ProtocolCatalog.name, Professional.name)
            .join(ProtocolCatalog, ProtocolCatalog.id == Assessment.protocol_id, isouter=True)
            .join(Professional, Professional.id == Assessment.professional_id)
            .where(*filters)
            .order_by(Assessment.date.desc(), Assessment.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(assessment.id),
            "kind": "assessment",
            "label": full_name or name or assessment.protocol_id,
            "date": assessment.date.isoformat(),
            "author_name": author_name,
            "protocol_id": assessment.protocol_id,
            "status": assessment.status,
            "summary": _trim(_assessment_summary(assessment), SOURCE_SUMMARY_MAX_CHARS),
        }
        for assessment, full_name, name, author_name in rows
    ]
    return items, total


async def _list_evolutions(db, *, patient_id, from_date, to_date, offset, limit):
    filters = [Evolution.patient_id == patient_id]
    if from_date is not None:
        filters.append(func.date(Evolution.date) >= from_date)
    if to_date is not None:
        filters.append(func.date(Evolution.date) <= to_date)
    total = int(await db.scalar(select(func.count()).select_from(Evolution).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(Evolution, Professional.name)
            .join(Professional, Professional.id == Evolution.professional_id)
            .where(*filters)
            .order_by(Evolution.date.desc(), Evolution.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(evolution.id),
            "kind": "evolution",
            "label": evolution.title or "Evolução",
            "date": _iso(evolution.date),
            "author_name": author_name,
            "protocol_id": None,
            "status": None,
            "summary": _trim(evolution.content, SOURCE_SUMMARY_MAX_CHARS),
        }
        for evolution, author_name in rows
    ]
    return items, total


async def _list_sessions(db, *, patient_id, from_date, to_date, offset, limit):
    filters = [Session.patient_id == patient_id]
    if from_date is not None:
        filters.append(func.date(Session.date) >= from_date)
    if to_date is not None:
        filters.append(func.date(Session.date) <= to_date)
    total = int(await db.scalar(select(func.count()).select_from(Session).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(Session, Professional.name)
            .join(Professional, Professional.id == Session.professional_id)
            .where(*filters)
            .order_by(Session.date.desc(), Session.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = []
    for session, author_name in rows:
        summary = session.notes or "; ".join(str(item) for item in (session.objectives or []))
        items.append(
            {
                "id": str(session.id),
                "kind": "session",
                "label": session.type or "Sessão",
                "date": _iso(session.date),
                "author_name": author_name,
                "protocol_id": None,
                "status": None,
                "summary": _trim(summary, SOURCE_SUMMARY_MAX_CHARS),
            }
        )
    return items, total


async def _list_goals(db, *, patient_id, from_date, to_date, offset, limit):
    filters = [Goal.patient_id == patient_id]
    if from_date is not None:
        filters.append(Goal.start_date >= from_date)
    if to_date is not None:
        filters.append(Goal.start_date <= to_date)
    total = int(await db.scalar(select(func.count()).select_from(Goal).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(Goal, Professional.name)
            .join(Professional, Professional.id == Goal.professional_id)
            .where(*filters)
            .order_by(Goal.start_date.desc(), Goal.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(goal.id),
            "kind": "goal",
            "label": goal.title,
            "date": goal.start_date.isoformat(),
            "author_name": author_name,
            "protocol_id": None,
            "status": None,
            "summary": _trim(f"{goal.area} — {goal.progress}% — {goal.status}", SOURCE_SUMMARY_MAX_CHARS),
        }
        for goal, author_name in rows
    ]
    return items, total


async def list_report_sources(
    db: AsyncSession,
    *,
    patient_id: UUID,
    kind: str,
    from_date: date | None = None,
    to_date: date | None = None,
    page: int = 1,
    limit: int = 20,
) -> tuple[list[dict], int]:
    if kind not in SOURCE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tipo de fonte inválido. Use assessment, evolution, session ou goal.",
        )
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Período inválido: a data inicial não pode ser posterior à data final.",
        )
    if page < 1 or limit < 1 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Paginação inválida.",
        )
    offset = (page - 1) * limit
    if kind == "assessment":
        return await _list_assessments(
            db, patient_id=patient_id, from_date=from_date, to_date=to_date, offset=offset, limit=limit
        )
    if kind == "evolution":
        return await _list_evolutions(
            db, patient_id=patient_id, from_date=from_date, to_date=to_date, offset=offset, limit=limit
        )
    if kind == "session":
        return await _list_sessions(
            db, patient_id=patient_id, from_date=from_date, to_date=to_date, offset=offset, limit=limit
        )
    return await _list_goals(
        db, patient_id=patient_id, from_date=from_date, to_date=to_date, offset=offset, limit=limit
    )


# --------------------------------------------------------------------------- #
# Creation (POST /ai/reports with type=consolidado)
# --------------------------------------------------------------------------- #


def _reject_duplicates(selection: ReportCompositionCreate) -> None:
    for values in (
        selection.assessment_ids,
        selection.evolution_ids,
        selection.session_ids,
        selection.goal_ids,
    ):
        if len(set(values)) != len(values):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A seleção contém fontes duplicadas.",
            )
    pairs = [(str(pair.base_id), str(pair.target_id)) for pair in selection.comparisons]
    for index, pair in enumerate(pairs):
        previous = pairs[:index]
        if pair in previous or (pair[1], pair[0]) in previous:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A seleção contém comparações duplicadas.",
            )


async def _load_selected(
    db: AsyncSession,
    *,
    patient_id: UUID,
    selection: ReportCompositionCreate,
    strict: bool,
    refresh: bool = False,
) -> dict[str, list]:
    """Load exactly the selected rows; never the rest of the record."""
    targets = {
        "assessments": (Assessment, list(dict.fromkeys(selection.assessment_ids))),
        "evolutions": (Evolution, list(dict.fromkeys(selection.evolution_ids))),
        "sessions": (Session, list(dict.fromkeys(selection.session_ids))),
        "goals": (Goal, list(dict.fromkeys(selection.goal_ids))),
    }
    loaded: dict[str, list] = {key: [] for key in targets}
    for key, (model, ids) in targets.items():
        if not ids:
            continue
        stmt = select(model).where(model.patient_id == patient_id, model.id.in_(ids))
        if refresh:
            stmt = stmt.execution_options(populate_existing=True)
        by_id = {str(row.id): row for row in (await db.scalars(stmt)).all()}
        for source_id in ids:
            row = by_id.get(str(source_id))
            if row is None:
                if strict:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Fonte selecionada não encontrada para este paciente.",
                    )
                continue
            loaded[key].append(row)
    return loaded


async def _load_protocols(db: AsyncSession, assessments: list[Assessment]) -> dict[str, ProtocolCatalog]:
    protocol_ids = {assessment.protocol_id for assessment in assessments}
    if not protocol_ids:
        return {}
    rows = (await db.scalars(select(ProtocolCatalog).where(ProtocolCatalog.id.in_(protocol_ids)))).all()
    return {protocol.id: protocol for protocol in rows}


async def _author_names(db: AsyncSession, rows: dict[str, list]) -> dict[str, str]:
    professional_ids = {row.professional_id for group in rows.values() for row in group}
    if not professional_ids:
        return {}
    result = await db.execute(
        select(Professional.id, Professional.name).where(Professional.id.in_(professional_ids))
    )
    return {str(professional_id): name for professional_id, name in result.all()}


async def _build_comparisons(
    db: AsyncSession,
    *,
    patient_id: UUID,
    selection: ReportCompositionCreate,
    assessments: list[Assessment],
) -> tuple[list[dict], list[str]]:
    by_id = {str(assessment.id): assessment for assessment in assessments}
    items: list[dict] = []
    texts: list[str] = []
    for pair in selection.comparisons:
        base_key, target_key = str(pair.base_id), str(pair.target_id)
        if base_key == target_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uma comparação precisa de duas avaliações diferentes.",
            )
        if base_key not in by_id or target_key not in by_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Comparação inválida: use apenas avaliações selecionadas neste laudo.",
            )
        if by_id[base_key].protocol_id != by_id[target_key].protocol_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Comparação inválida: as avaliações precisam ser do mesmo protocolo.",
            )
        result = await compare_assessments(
            db, patient_id=patient_id, base_id=pair.base_id, target_id=pair.target_id
        )
        items.append(
            {
                "baseId": result.base.id,
                "targetId": result.target.id,
                "percentageDelta": result.percentage_delta,
                "metrics": [
                    {
                        "key": metric.key,
                        "label": metric.label,
                        "base": metric.base,
                        "target": metric.target,
                        "delta": metric.delta,
                    }
                    for metric in result.metrics
                ],
            }
        )
        texts.append(_comparison_text(result))
    return items, texts


async def _capture(
    db: AsyncSession,
    *,
    patient_id: UUID,
    selection: ReportCompositionCreate,
    rows: dict[str, list],
    comparison_items: list[dict],
    comparison_texts: list[str],
) -> CompositionCapture:
    identity = await build_identity_section(db, patient_id)
    protocols = await _load_protocols(db, rows["assessments"])
    authors = await _author_names(db, rows)

    sources: list[dict] = []
    source_hashes: dict[str, str] = {}
    warnings: list[str] = []
    instrument_lines: list[str] = []
    parts: list[str] = []

    if identity:
        parts.append(f"### Identificação\n{identity}")

    detail_blocks: list[str] = []
    for assessment in rows["assessments"]:
        protocol = protocols.get(assessment.protocol_id)
        label = protocol.full_name if protocol and protocol.full_name else assessment.protocol_id
        author = authors.get(str(assessment.professional_id), "") or "não informado"
        block = _assessment_block(assessment, label, author)
        detail_blocks.append(block)
        source_hashes[_source_key("assessment", assessment.id)] = _sha256(block)
        sources.append(
            {
                "kind": "assessment",
                "id": str(assessment.id),
                "label": label,
                "date": assessment.date.isoformat(),
                "authorName": author,
                "protocolId": assessment.protocol_id,
                "sourceHash": _sha256(block),
            }
        )
        instrument_lines.append(_instrument_line(assessment, label, author))
        warning = _norm_warning(label, assessment.scores)
        if warning:
            warnings.append(warning)

    if instrument_lines:
        parts.append("### Instrumentos selecionados\n" + "\n".join(instrument_lines))
    if detail_blocks:
        parts.append("### Resultados dos instrumentos\n" + "\n\n".join(detail_blocks))

    kind_specs = (
        ("evolutions", "evolution", "Evoluções selecionadas", _evolution_block, lambda row: row.title or "Evolução", lambda row: _iso(row.date)),
        ("sessions", "session", "Sessões selecionadas", _session_block, lambda row: row.type or "Sessão", lambda row: _iso(row.date)),
        ("goals", "goal", "Metas selecionadas", _goal_block, lambda row: row.title, lambda row: row.start_date.isoformat()),
    )
    for key, kind, header, block_builder, label_for, date_for in kind_specs:
        blocks: list[str] = []
        for row in rows[key]:
            author = authors.get(str(row.professional_id), "") or "não informado"
            block = block_builder(row, author)
            blocks.append(block)
            source_hashes[_source_key(kind, row.id)] = _sha256(block)
            sources.append(
                {
                    "kind": kind,
                    "id": str(row.id),
                    "label": label_for(row),
                    "date": date_for(row),
                    "authorName": author,
                    "protocolId": None,
                    "sourceHash": _sha256(block),
                }
            )
        if blocks:
            parts.append(f"### {header}\n" + "\n\n".join(blocks))

    if comparison_texts:
        parts.append("### Comparações validadas\n" + "\n".join(comparison_texts))
    if warnings:
        parts.append("### Avisos sobre normas\n" + "\n".join(f"- {warning}" for warning in warnings))

    context = "\n\n".join(parts)
    return CompositionCapture(
        sources=sources,
        comparisons=comparison_items,
        warnings=warnings,
        context=context,
        source_hashes=source_hashes,
        context_hash=_sha256(context),
        identity=identity,
        instruments="\n".join(instrument_lines),
    )


def _job_input(body: AIReportCreate) -> dict:
    selection = body.composition
    return {
        "patientId": body.patient_id,
        "type": body.type,
        "assessmentIds": [str(item) for item in selection.assessment_ids],
        "evolutionIds": [str(item) for item in selection.evolution_ids],
        "sessionIds": [str(item) for item in selection.session_ids],
        "goalIds": [str(item) for item in selection.goal_ids],
        "comparisons": [
            {"baseId": str(pair.base_id), "targetId": str(pair.target_id)}
            for pair in selection.comparisons
        ],
        "supersedesReportId": (
            str(selection.supersedes_report_id) if selection.supersedes_report_id else None
        ),
    }


async def create_consolidated_report(
    db: AsyncSession,
    *,
    professional: Professional,
    body: AIReportCreate,
) -> tuple[AIReport, AIReportComposition, Patient]:
    selection = body.composition
    if selection is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="O campo composition é obrigatório para relatórios do tipo consolidado.",
        )
    try:
        patient_id = UUID(body.patient_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="patientId inválido."
        ) from exc

    patient = await db.scalar(
        select(Patient).where(Patient.id == patient_id, Patient.professional_id == professional.id)
    )
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado")

    _reject_duplicates(selection)
    rows = await _load_selected(db, patient_id=patient.id, selection=selection, strict=True)
    assessments = rows["assessments"]

    if len({assessment.protocol_id for assessment in assessments}) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Selecione avaliações de pelo menos dois protocolos diferentes.",
        )
    if any(assessment.status != ASSESSMENT_STATUS_COMPLETED for assessment in assessments):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Só é possível incluir avaliações concluídas no laudo consolidado.",
        )

    comparison_items, comparison_texts = await _build_comparisons(
        db, patient_id=patient.id, selection=selection, assessments=assessments
    )

    if selection.supersedes_report_id is not None:
        superseded = await db.scalar(
            select(AIReport).where(
                AIReport.id == selection.supersedes_report_id,
                AIReport.professional_id == professional.id,
                AIReport.patient_id == patient.id,
            )
        )
        if superseded is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Relatório substituído não encontrado"
            )

    capture = await _capture(
        db,
        patient_id=patient.id,
        selection=selection,
        rows=rows,
        comparison_items=comparison_items,
        comparison_texts=comparison_texts,
    )

    max_chars = get_settings().ai_context_max_chars
    if len(capture.context) > max_chars:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A seleção excede o orçamento de contexto ({max_chars} caracteres). "
                "Reduza a seleção e tente novamente."
            ),
        )

    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=patient.id,
        job_type="report",
        input_data=_job_input(body),
    )
    spec = AI_TOOL_SPECS[f"report:{CONSOLIDATED_REPORT_TYPE}"]
    prompt = build_tool_prompt(spec, context=capture.context, extra_prompt=body.prompt)
    drafted = (await run_llm(prompt, spec.system, output=spec.output) or "").strip()
    if not drafted:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="O provedor de IA não retornou conteúdo para o laudo. Tente novamente.",
            headers={"Retry-After": "60"},
        )

    content = compose_consolidated_content(
        identity=capture.identity, instruments=capture.instruments, drafted=drafted
    )

    # Revalidate scope/state/hash after the LLM finished — without ever holding a
    # database lock during the provider call.
    still_owned = await db.scalar(
        select(Patient.id).where(
            Patient.id == patient.id, Patient.professional_id == professional.id
        )
    )
    if still_owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado")
    fresh_rows = await _load_selected(
        db, patient_id=patient.id, selection=selection, strict=False, refresh=True
    )
    recheck = await _capture(
        db,
        patient_id=patient.id,
        selection=selection,
        rows=fresh_rows,
        comparison_items=comparison_items,
        comparison_texts=comparison_texts,
    )
    if recheck.source_hashes != capture.source_hashes or recheck.context_hash != capture.context_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="As fontes selecionadas mudaram durante a geração. Gere o laudo novamente.",
        )
    if any(
        assessment.status != ASSESSMENT_STATUS_COMPLETED
        for assessment in fresh_rows["assessments"]
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uma avaliação selecionada deixou de estar concluída. Gere o laudo novamente.",
        )

    preview = content[:200] + "..." if len(content) > 200 else content
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type=CONSOLIDATED_REPORT_TYPE,
        date=date.today(),
        preview=preview,
        content=content,
        status="draft",
        version=1,
    )
    db.add(report)
    await db.flush()

    composition = AIReportComposition(
        report_id=report.id,
        professional_id=professional.id,
        selection={
            "assessmentIds": [str(item) for item in selection.assessment_ids],
            "evolutionIds": [str(item) for item in selection.evolution_ids],
            "sessionIds": [str(item) for item in selection.session_ids],
            "goalIds": [str(item) for item in selection.goal_ids],
            "comparisons": [
                {"baseId": str(pair.base_id), "targetId": str(pair.target_id)}
                for pair in selection.comparisons
            ],
            "supersedesReportId": (
                str(selection.supersedes_report_id) if selection.supersedes_report_id else None
            ),
        },
        snapshot={
            "sources": capture.sources,
            "comparisons": capture.comparisons,
            "warnings": capture.warnings,
        },
        source_hashes=capture.source_hashes,
        context_hash=capture.context_hash,
        template_version=TEMPLATE_VERSION,
        captured_at=utcnow(),
        supersedes_report_id=selection.supersedes_report_id,
    )
    db.add(composition)
    await db.flush()

    job.status = "completed"
    job.result = json.dumps({"reportId": str(report.id)})
    job.completed_at = utcnow()
    await db.flush()
    await create_timeline_event(
        db,
        patient_id=patient.id,
        professional_id=professional.id,
        event_type="relatorio",
        title="Laudo consolidado gerado por IA",
        description=preview,
        source_id=report.id,
    )
    await db.commit()
    return report, composition, patient


async def get_report_composition(
    db: AsyncSession, *, report_id: UUID, professional: Professional
) -> AIReportComposition:
    composition = await db.scalar(
        select(AIReportComposition)
        .join(AIReport, AIReport.id == AIReportComposition.report_id)
        .where(
            AIReportComposition.report_id == report_id,
            AIReport.professional_id == professional.id,
        )
    )
    if composition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Composição não encontrada para este relatório.",
        )
    return composition
