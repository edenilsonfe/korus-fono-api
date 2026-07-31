"""C2 — ScoringSession: NormalizedScores behind a small interface.

 YAGNI slice: manifest + precomputed-scores adapters. Battery remain
behind their own engines until a later deepen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ScoreError(ValueError):
    """Entrada inválida / mode sem engine — capturado na borda HTTP."""


@dataclass(frozen=True)
class NormalizedScores:
    result: str
    percentage: int
    interpretation: str
    fields: list[dict[str, str]]
    raw_scores: dict[str, Any] = field(default_factory=dict)
    norms: dict[str, Any] | None = None

    def to_assessment_fields(self) -> list[dict[str, str]]:
        return list(self.fields)


def _scores_to_assessment_fields(scores: dict[str, Any]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    domains = scores.get("domains") or {}
    for key, value in domains.items():
        fields.append({"label": str(key), "value": str(value)})
    if scores.get("level_label"):
        fields.append({"label": "Nível", "value": str(scores["level_label"])})
    return fields


def _scores_to_percentage(scores: dict[str, Any]) -> int:
    total = scores.get("total")
    if total is None:
        pct = scores.get("percentage")
        if pct is None:
            return 0
        try:
            return int(round(float(pct)))
        except (TypeError, ValueError):
            return 0
    try:
        value = float(total)
    except (TypeError, ValueError):
        return 0
    # Ordinal FOIS-like (1–7) before treating as already-percent 0–100.
    if 0 < value <= 7:
        return int(round((value / 7) * 100))
    if 0 <= value <= 100:
        return int(round(value))
    return min(100, int(round(value)))


def normalize_scores(scores: dict[str, Any]) -> NormalizedScores:
    norms = scores.get("norms_status")
    if norms is not None and not isinstance(norms, dict):
        norms = None
    return NormalizedScores(
        result=str(scores.get("summary") or scores.get("total") or "Concluído"),
        percentage=_scores_to_percentage(scores),
        interpretation=str(
            scores.get("interpretation") or scores.get("detail") or scores.get("summary") or ""
        ),
        fields=_scores_to_assessment_fields(scores),
        raw_scores=scores,
        norms=norms,
    )


class ScoringSession:
    def __init__(
        self,
        *,
        protocol_id: str | None,
        scoring_mode: str,
        precomputed: dict[str, Any] | None = None,
    ) -> None:
        self.protocol_id = protocol_id
        self.scoring_mode = scoring_mode
        self._precomputed = precomputed

    @classmethod
    def from_protocol(cls, protocol_id: str, scoring_mode: str) -> ScoringSession:
        return cls(protocol_id=protocol_id, scoring_mode=scoring_mode)

    @classmethod
    def from_scores(cls, scores: dict[str, Any]) -> ScoringSession:
        """Fixture / battery adapter: scores already computed by an engine."""
        return cls(protocol_id=None, scoring_mode="precomputed", precomputed=scores)

    def score(self, answers: dict[str, Any]) -> NormalizedScores:
        if self._precomputed is not None:
            return normalize_scores(self._precomputed)

        if self.scoring_mode != "manifest":
            raise ScoreError(
                f"ScoringSession ainda não cobre mode={self.scoring_mode!r} "
                f"(protocol={self.protocol_id!r})"
            )
        assert self.protocol_id is not None
        try:
            from app.core.instrument_aliases import resolve_instrument_slug
            from app.services.instrument_content_package import get_instrument_content_package
            from app.services.instrument_scoring_service import InstrumentScoringService

            slug = resolve_instrument_slug(self.protocol_id)
            if not slug:
                raise ScoreError(f"Protocolo '{self.protocol_id}' não possui pacote de instrumento")
            package = get_instrument_content_package(slug)
            raw = InstrumentScoringService.score(package, answers)
            return normalize_scores(raw)
        except ScoreError:
            raise
        except FileNotFoundError as exc:
            raise ScoreError(str(exc)) from exc
        except ValueError as exc:
            raise ScoreError(str(exc)) from exc

    def to_assessment_fields(self) -> list[dict[str, str]]:
        """Requires a prior .score() — prefer NormalizedScores.to_assessment_fields()."""
        raise ScoreError("Use NormalizedScores.to_assessment_fields() após .score()")
