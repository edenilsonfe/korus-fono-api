"""Tests for ABFW battery scoring engines."""

from app.services.battery_scoring_service import (
    score_fluency_module,
    score_phonology_module,
    score_pragmatics_module,
    score_vocabulary_module,
    synthesize_battery_scores,
)
from app.services.instrument_content_package import get_instrument_content_package


def _package():
    return get_instrument_content_package("abfw")


def test_phonology_scoring_counts_processes():
    package = _package()
    answers = {item["id"]: {"classification": "correct", "processes": []} for item in package.get_module_items("fonologia-imitacao")}
    answers.update({
        "fon_im_01": {"response": "pato", "classification": "correct", "processes": []},
        "fon_im_02": {"response": "papo", "classification": "substitution", "processes": ["fronting"]},
    })
    result = score_phonology_module(
        package, "fonologia-imitacao", answers, patient_age_months=72
    )
    assert result["correct"] == len(answers) - 1
    assert result["altered"] == 1
    assert any(p["id"] == "fronting" for p in result["processes"])


def test_vocabulary_scoring_dvu_percentages():
    package = _package()
    items = package.get_module_items("vocabulario")[:5]
    answers = {
        items[0]["id"]: {"classification": "dvu"},
        items[1]["id"]: {"classification": "nd"},
        items[2]["id"]: {"classification": "ps"},
        items[3]["id"]: {"classification": "dvu"},
        items[4]["id"]: {"classification": "dvu"},
    }
    result = score_vocabulary_module(
        package, "vocabulario", answers, patient_age_months=48
    )
    assert result["dvu"] == 3
    # The denominator is the complete manifest, including unanswered items.
    total = len(package.get_module_items("vocabulario"))
    assert result["total_items"] == total
    assert result["percentage"] == round(3 / total * 100, 1)


def test_fluency_scoring_calculates_rates():
    package = _package()
    answers = {
        "flu_session": {
            "duration_seconds": 120,
            "syllable_count": 200,
            "word_count": 80,
            "sample_type": "reading",
        },
        "flu_syllable_repetition": {"count": 3},
        "flu_block": {"count": 1},
        "flu_interjection": {"count": 2},
    }
    result = score_fluency_module(package, "fluencia", answers)
    assert result["syllables_per_minute"] == 100.0
    assert result["words_per_minute"] == 40.0
    assert result["disfluency_count"] == 6
    assert result["stuttering_like_count"] == 4
    assert result["common_disfluency_count"] == 2
    assert result["stuttering_like_percentage"] == 2.0
    assert result["common_disfluency_percentage"] == 1.0
    assert result["disfluency_percentage"] == 3.0
    assert result["sample_type"] == "reading"
    assert result["rate_status"] == "below"  # 100 < 120
    assert result["level"] == "adequate"  # SLD% <= 3
    assert "percentage" in result


def test_fluency_scoring_levels_by_sld_not_total():
    package = _package()
    # Muitas comuns, poucas SLD → adequado por SLD
    answers = {
        "flu_session": {"duration_seconds": 60, "syllable_count": 200, "word_count": 90},
        "flu_interjection": {"count": 10},
        "flu_revision": {"count": 8},
        "flu_block": {"count": 2},
    }
    result = score_fluency_module(package, "fluencia", answers)
    assert result["stuttering_like_percentage"] == 1.0
    assert result["disfluency_percentage"] == 10.0
    assert result["level"] == "adequate"


def test_fluency_scoring_altered_when_sld_high():
    package = _package()
    answers = {
        "flu_session": {"duration_seconds": 60, "syllable_count": 100, "word_count": 40},
        "flu_syllable_repetition": {"count": 4},
        "flu_prolongation": {"count": 3},
    }
    result = score_fluency_module(package, "fluencia", answers)
    assert result["stuttering_like_percentage"] == 7.0
    assert result["level"] == "altered"
    assert result["rate_status"] == "below"  # 100 síl/min < 120


def test_pragmatics_scoring_checklist():
    package = _package()
    items = package.get_module_items("pragmatica")[:3]
    answers = {
        items[0]["id"]: {"classification": "adequate"},
        items[1]["id"]: {"classification": "attention"},
        items[2]["id"]: {"classification": "altered"},
    }
    result = score_pragmatics_module(package, "pragmatica", answers)
    assert result["counts"]["adequate"] == 1
    assert result["counts"]["attention"] == 1
    assert result["counts"]["altered"] == 1


def test_synthesize_battery_scores():
    package = _package()
    subforms = [
        score_phonology_module(
            package,
            "fonologia-imitacao",
            {"fon_im_01": {"classification": "correct", "processes": []}},
            patient_age_months=60,
        ),
        score_vocabulary_module(
            package,
            "vocabulario",
            {"voc_animais_01": {"classification": "dvu"}},
            patient_age_months=60,
        ),
    ]
    synthesized = synthesize_battery_scores(package, subforms)
    assert "domains" in synthesized
    assert synthesized["percentage"] >= 0
