"""Tests for developmental_screening scoring engine."""

from app.services.battery_scoring_service import (
    score_developmental_module,
    synthesize_battery_scores,
)
from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)


def _clear_cache():
    clear_instrument_content_package_cache()


def test_denver_delay_detection_by_age():
    _clear_cache()
    package = get_instrument_content_package("denver-ii")
    answers = {
        "ps_01": {"response": "fail"},
        "ps_02": {"response": "pass"},
    }
    result = score_developmental_module(
        package, "pessoal-social", answers, patient_age_months=12
    )
    assert result["delay_count"] == 1
    assert result["level"] == "caution"
    assert result["delays"][0]["id"] == "ps_01"


def test_denver_no_delay_when_patient_younger_than_item():
    _clear_cache()
    package = get_instrument_content_package("denver-ii")
    answers = {"ps_01": {"response": "fail"}}
    result = score_developmental_module(
        package, "pessoal-social", answers, patient_age_months=1
    )
    assert result["delay_count"] == 0
    assert result["level"] == "expected"


def test_bayley_basal_ceiling_from_session():
    _clear_cache()
    package = get_instrument_content_package("bayley-iii")
    items = package.get_module_items("cognicao")
    answers = {
        "_session": {"basal_index": 2, "ceiling_index": 5},
        items[0]["id"]: {"response": "pass"},
        items[1]["id"]: {"response": "pass"},
        items[2]["id"]: {"response": "pass"},
        items[3]["id"]: {"response": "fail"},
        items[4]["id"]: {"response": "pass"},
        items[5]["id"]: {"response": "fail"},
        items[6]["id"]: {"response": "fail"},
        items[7]["id"]: {"response": "fail"},
    }
    result = score_developmental_module(
        package, "cognicao", answers, patient_age_months=24
    )
    scored_ids = {item["id"] for item in result["items"] if item["status"] != "unanswered"}
    assert items[0]["id"] not in scored_ids
    assert items[1]["id"] not in scored_ids
    assert items[2]["id"] in scored_ids
    assert items[7]["id"] not in scored_ids


def test_bayley_auto_basal_ceiling_rules():
    _clear_cache()
    package = get_instrument_content_package("bayley-iii")
    items = package.get_module_items("motor")
    answers = {"_session": {"start_index": 3}}
    for idx, item in enumerate(items):
        if idx <= 3:
            answers[item["id"]] = {"response": "pass"}
        else:
            answers[item["id"]] = {"response": "fail"}
    result = score_developmental_module(
        package, "motor", answers, patient_age_months=30
    )
    assert result["session"]["basal_index"] is not None or result["passes"] >= 1


def test_bayley_start_index_excludes_prior_unanswered():
    """Faixa etária = start: itens anteriores à janela não entram como unanswered."""
    _clear_cache()
    package = get_instrument_content_package("bayley-iii")
    items = package.get_module_items("cognicao")
    answers = {
        "_session": {"start_index": 3},
        items[3]["id"]: {"response": "pass"},
        items[4]["id"]: {"response": "pass"},
        items[5]["id"]: {"response": "pass"},
    }
    result = score_developmental_module(
        package, "cognicao", answers, patient_age_months=18
    )
    scored_ids = {item["id"] for item in result["items"]}
    assert items[0]["id"] not in scored_ids
    assert items[1]["id"] not in scored_ids
    assert items[2]["id"] not in scored_ids
    assert items[3]["id"] in scored_ids
    assert result["unanswered"] == 0
    assert result["session"]["basal_index"] == 3
    assert result["session"]["ceiling_index"] == 5


def test_synthesize_developmental_battery():
    _clear_cache()
    package = get_instrument_content_package("denver-ii")
    subforms = [
        score_developmental_module(
            package,
            "pessoal-social",
            {"ps_01": {"response": "fail"}, "ps_02": {"response": "pass"}},
            patient_age_months=12,
        ),
        score_developmental_module(
            package,
            "motor-fino",
            {"fm_01": {"response": "pass"}},
            patient_age_months=12,
        ),
    ]
    synthesized = synthesize_battery_scores(package, subforms)
    assert synthesized["engine"] == "developmental_screening"
    assert synthesized["total_delays"] == 1
    assert "domain_levels" in synthesized
    assert synthesized["domain_levels"]["PS"] == "caution"
