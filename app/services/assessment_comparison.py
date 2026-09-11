"""Compare two completed assessments of the same protocol (reavaliação)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Assessment, ProtocolCatalog

MAX_CHANGED_ITEMS = 50

# Keys that never become comparison metrics (metadata, not scores).
_NON_METRIC_KEYS = frozenset(
    {
        "summary",
        "interpretation",
        "detail",
        "level_label",
        "norms_status",
        "percentage",
        "clinical_conclusion",
        "setup",
        "domains",
    }
)

# When a score value is a mapping, look for the number under these keys, in order.
_NESTED_VALUE_KEYS = ("score", "value", "total", "percentage", "raw")


@dataclass(frozen=True)
class AssessmentComparisonSide:
    id: str
    date: str
    result: str
    percentage: int


@dataclass(frozen=True)
class AssessmentComparisonMetric:
    key: str
    label: str
    base: float | None = None
    target: float | None = None
    delta: float | None = None


@dataclass(frozen=True)
class AssessmentChangedItem:
    key: str
    base: Any = None
    target: Any = None


@dataclass(frozen=True)
class AssessmentComparisonResult:
    protocol_id: str
    protocol_name: str
    base: AssessmentComparisonSide
    target: AssessmentComparisonSide
    percentage_delta: int
    metrics: list[AssessmentComparisonMetric] = field(default_factory=list)
    answers_changed: int = 0
    answers_total: int = 0
    changed_items: list[AssessmentChangedItem] = field(default_factory=list)
    summary: str = ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, dict):
        for nested_key in _NESTED_VALUE_KEYS:
            if nested_key in value:
                return _as_number(value[nested_key])
    return None


def extract_numeric_metrics(scores: dict | None) -> dict[str, float]:
    """Flatten assessment scores into {key: number}.

    Prefers the `domains` mapping when present; otherwise uses top-level numeric
    entries, skipping known metadata keys. A top-level `total` is kept as a
    metric in both shapes.
    """
    if not isinstance(scores, dict):
        return {}
    domains = scores.get("domains")
    source = domains if isinstance(domains, dict) else scores
    metrics: dict[str, float] = {}
    for key, value in source.items():
        if key in _NON_METRIC_KEYS:
            continue
        number = _as_number(value)
        if number is not None:
            metrics[str(key)] = number
    total = _as_number(scores.get("total"))
    if total is not None:
        metrics.setdefault("total", total)
    return metrics


def _label_for(key: str) -> str:
    return key.replace("_", " ")


def _side(assessment: Assessment) -> AssessmentComparisonSide:
    return AssessmentComparisonSide(
        id=str(assessment.id),
        date=assessment.date.isoformat(),
        result=assessment.result or "",
        percentage=int(assessment.percentage or 0),
    )


def _changed_answer_items(
    base_answers: dict | None, target_answers: dict | None
) -> tuple[int, int, list[AssessmentChangedItem]]:
    base = base_answers if isinstance(base_answers, dict) else {}
    target = target_answers if isinstance(target_answers, dict) else {}
    keys = list(dict.fromkeys([*base.keys(), *target.keys()]))
    changed: list[AssessmentChangedItem] = []
    changed_count = 0
    for key in keys:
        base_value = base.get(key)
        target_value = target.get(key)
        if base_value == target_value:
            continue
        changed_count += 1
        if len(changed) < MAX_CHANGED_ITEMS:
            changed.append(
                AssessmentChangedItem(key=str(key), base=base_value, target=target_value)
            )
    return changed_count, len(keys), changed


def _build_summary(
    protocol_name: str,
    base: AssessmentComparisonSide,
    target: AssessmentComparisonSide,
    percentage_delta: int,
) -> str:
    if percentage_delta == 0:
        return (
            f"{protocol_name}: resultado estável em {target.percentage}% "
            f"entre {base.date} e {target.date}."
        )
    return (
        f"{protocol_name}: {base.percentage}% em {base.date} "
        f"→ {target.percentage}% em {target.date} "
        f"({percentage_delta:+d} pontos percentuais)."
    )


def _sort_pair(
    first: Assessment, second: Assessment
) -> tuple[Assessment, Assessment]:
    """Return (base, target) with base never newer than target."""
    if (first.date, first.created_at) <= (second.date, second.created_at):
        return first, second
    return second, first


async def _load_completed(
    db: AsyncSession, *, patient_id: UUID, assessment_id: UUID, label: str
) -> Assessment:
    assessment = await db.scalar(
        select(Assessment).where(
            Assessment.id == assessment_id,
            Assessment.patient_id == patient_id,
        )
    )
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Avaliação {label} não encontrada para este paciente.",
        )
    if assessment.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Só é possível comparar avaliações concluídas.",
        )
    return assessment


async def compare_assessments(
    db: AsyncSession,
    *,
    patient_id: UUID,
    base_id: UUID,
    target_id: UUID,
) -> AssessmentComparisonResult:
    if base_id == target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selecione duas avaliações diferentes para comparar.",
        )
    first = await _load_completed(
        db, patient_id=patient_id, assessment_id=base_id, label="base"
    )
    second = await _load_completed(
        db, patient_id=patient_id, assessment_id=target_id, label="comparada"
    )
    if first.protocol_id != second.protocol_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="As avaliações precisam ser do mesmo protocolo.",
        )
    base, target = _sort_pair(first, second)

    protocol = await db.get(ProtocolCatalog, base.protocol_id)
    protocol_name = protocol.name if protocol else base.protocol_id
    protocol_id = base.protocol_id

    base_metrics = extract_numeric_metrics(base.scores)
    target_metrics = extract_numeric_metrics(target.scores)
    metrics: list[AssessmentComparisonMetric] = []
    for key in dict.fromkeys([*base_metrics.keys(), *target_metrics.keys()]):
        base_value = base_metrics.get(key)
        target_value = target_metrics.get(key)
        delta = (
            target_value - base_value
            if base_value is not None and target_value is not None
            else None
        )
        metrics.append(
            AssessmentComparisonMetric(
                key=key,
                label=_label_for(key),
                base=base_value,
                target=target_value,
                delta=delta,
            )
        )

    answers_changed, answers_total, changed_items = _changed_answer_items(
        base.answers, target.answers
    )

    base_side = _side(base)
    target_side = _side(target)
    percentage_delta = target_side.percentage - base_side.percentage

    return AssessmentComparisonResult(
        protocol_id=protocol_id,
        protocol_name=protocol_name,
        base=base_side,
        target=target_side,
        percentage_delta=percentage_delta,
        metrics=metrics,
        answers_changed=answers_changed,
        answers_total=answers_total,
        changed_items=changed_items,
        summary=_build_summary(protocol_name, base_side, target_side, percentage_delta),
    )
