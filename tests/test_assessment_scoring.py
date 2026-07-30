"""Tests for assessment scoring helpers and protocol modes."""

import pytest

from app.core.instrument_aliases import (
    CLIENT_SCORED_PROTOCOLS,
    SPM_PROTOCOL,
    has_manifest_package,
    instrument_slug_for_protocol,
    resolve_instrument_slug,
    resolve_protocol_id,
)
from app.services.assessment_scoring import (
    build_assessment_from_scores,
    get_protocol_scoring_mode,
    score_manifest_protocol,
    scores_to_percentage,
)


@pytest.mark.parametrize(
    ("protocol_id", "expected_mode"),
    [
        ("spm", "spm"),
        ("fois", "manifest"),
        ("mchat", "client"),
    ],
)
def test_get_protocol_scoring_mode(protocol_id: str, expected_mode: str):
    assert get_protocol_scoring_mode(protocol_id) == expected_mode


def test_resolve_instrument_slug_aliases():
    assert resolve_instrument_slug("ados2") == "ados-2"
    assert resolve_instrument_slug("vbmapp") == "vb-mapp"


def test_eat10_masa_removed_from_aliases():
    assert resolve_instrument_slug("eat10") is None
    assert resolve_instrument_slug("masa") is None
    assert resolve_protocol_id("eat-10") == "eat-10"
    assert resolve_protocol_id("masa") == "masa"


def test_resolve_protocol_id_aliases():
    assert resolve_protocol_id("ados-2") == "ados2"
    assert resolve_protocol_id("denver-ii") == "denver2"
    assert resolve_protocol_id("abfw") == "abfw"
    assert instrument_slug_for_protocol("ados2") == "ados-2"
    assert instrument_slug_for_protocol("denver2") == "denver-ii"


def test_has_manifest_package():
    assert has_manifest_package("fois") is True
    assert has_manifest_package("m-chat") is False


def test_client_scored_protocols_contains_rastreios():
    assert "mchat" in CLIENT_SCORED_PROTOCOLS
    assert "portage" in CLIENT_SCORED_PROTOCOLS


def test_score_manifest_fois():
    scores = score_manifest_protocol("fois", {"fois_level": 5})
    assert scores["engine"] == "scaled_sum"
    assert scores["total"] == 5


def test_build_assessment_from_scores():
    derived = build_assessment_from_scores(
        {"summary": "Risco moderado", "total": 65, "domains": {"A": "ok"}}
    )
    assert derived["result"] == "Risco moderado"
    assert derived["percentage"] == 65
    assert len(derived["fields"]) >= 1


@pytest.mark.parametrize(
    ("total", "expected"),
    [(65, 65), (4, 57), ("invalid", 0)],
)
def test_scores_to_percentage(total, expected):
    assert scores_to_percentage({"total": total}) == expected


def test_spm_protocol_constant():
    assert SPM_PROTOCOL == "spm"
