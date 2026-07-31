from typing import Any

from app.core.instrument_aliases import (
    CLIENT_SCORED_PROTOCOLS,
    has_manifest_package,
    resolve_instrument_slug,
)
from app.services.instrument_content_package import get_instrument_content_package
from app.services.scoring_session import ScoringSession, ScoreError, normalize_scores


def get_protocol_scoring_mode(protocol_id: str) -> str:
    pid = protocol_id.lower()
    if pid == "abfw":
        return "battery"
    if has_manifest_package(pid):
        slug = resolve_instrument_slug(pid)
        if slug:
            try:
                package = get_instrument_content_package(slug)
                if package.data.get("supports_multi_session") and package.archetype in (
                    "battery",
                    "observational",
                ):
                    return "battery"
            except FileNotFoundError:
                pass
        return "manifest"
    if pid in CLIENT_SCORED_PROTOCOLS:
        return "client"
    return "manual"


def score_manifest_protocol(protocol_id: str, answers: dict[str, Any]) -> dict[str, Any]:
    try:
        return ScoringSession.from_protocol(protocol_id, "manifest").score(answers).raw_scores
    except ScoreError as exc:
        raise ValueError(str(exc)) from exc


def scores_to_assessment_fields(scores: dict[str, Any]) -> list[dict[str, str]]:
    return normalize_scores(scores).fields


def scores_to_percentage(scores: dict[str, Any]) -> int:
    return normalize_scores(scores).percentage


def build_assessment_from_scores(scores: dict[str, Any]) -> dict[str, Any]:
    n = normalize_scores(scores)
    return {
        "result": n.result,
        "percentage": n.percentage,
        "interpretation": n.interpretation,
        "fields": n.fields,
    }
