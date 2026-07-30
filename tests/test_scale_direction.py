"""Tests for observational scale_direction (lower_is_better inversion)."""

from app.services.battery_scoring_service import (
    score_observational_module,
    synthesize_battery_scores,
)
from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)


def _clear_cache():
    clear_instrument_content_package_cache()


def test_mbgr_lower_is_better_inverts_severe_scores():
    _clear_cache()
    package = get_instrument_content_package("mbgr")
    items = package.get_module_items("face")[:2]
    answers = {
        items[0]["id"]: {"value": 3},
        items[1]["id"]: {"value": 3},
    }
    result = score_observational_module(package, "face", answers)
    assert result["scale_direction"] == "lower_is_better"
    assert result["percentage"] == 0.0
    assert result["level"] == "altered"


def test_mbgr_lower_is_better_rewards_low_scores():
    _clear_cache()
    package = get_instrument_content_package("mbgr")
    items = package.get_module_items("face")[:2]
    answers = {
        items[0]["id"]: {"value": 0},
        items[1]["id"]: {"value": 1},
    }
    result = score_observational_module(package, "face", answers)
    assert result["percentage"] == 83.3
    assert result["level"] == "expected"


def test_pard_consistency_lower_is_better():
    _clear_cache()
    package = get_instrument_content_package("pard")
    items = package.get_module_items("liquido-fino")[:2]
    answers = {
        items[0]["id"]: {"value": 0},
        items[1]["id"]: {"value": 0},
    }
    good = score_observational_module(package, "liquido-fino", answers)
    assert good["percentage"] == 100.0

    answers_bad = {
        items[0]["id"]: {"value": 3},
        items[1]["id"]: {"value": 3},
    }
    bad = score_observational_module(package, "liquido-fino", answers_bad)
    assert bad["percentage"] == 0.0
    assert bad["level"] == "altered"


def test_pard_engasgo_everywhere_risco_elevado_not_strengths():
    """Engasgo em todas as consistências + checklist cheio → risco elevado; dificuldades ≠ strengths."""
    _clear_cache()
    package = get_instrument_content_package("pard")
    consistency_slugs = ["liquido-fino", "nectar", "mel", "pudim", "solido"]
    subforms: list[dict] = []
    for slug in consistency_slugs:
        items = package.get_module_items(slug)
        answers = {item["id"]: {"value": 3} for item in items}
        scored = score_observational_module(package, slug, answers)
        assert scored["level"] == "altered"
        assert scored["level_label"] == "Risco elevado"
        assert scored["strengths"] == []
        for item in items:
            assert item.get("text", item["id"]) in scored["attention_items"]
        subforms.append(scored)

    checklist_item = package.get_module_items("sinais-clinicos")[0]
    option_ids = [opt["id"] for opt in checklist_item["options"]]
    sinais = score_observational_module(
        package,
        "sinais-clinicos",
        {checklist_item["id"]: {"selected": option_ids}},
    )
    assert sinais["strengths"] == []
    assert checklist_item.get("text", checklist_item["id"]) in sinais["attention_items"]
    subforms.append(sinais)

    synth = synthesize_battery_scores(package, subforms)
    assert synth["percentage"] == 0.0
    assert synth["domains"]["consistencias"]["level"] == "altered"
    assert (synth.get("strengths") or []) == []
    assert len(synth.get("attention_items") or []) > 0


def test_amiofe_higher_is_better_keeps_max_as_strength():
    """higher_is_better: pontuação máxima continua sendo ponto forte."""
    _clear_cache()
    package = get_instrument_content_package("amiofe")
    items = package.get_module_items("face")[:2]
    scale = package.get_module_config("face").get("scale") or package.scale
    max_value = max(int(s["value"]) for s in scale)
    answers = {items[0]["id"]: {"value": max_value}, items[1]["id"]: {"value": max_value}}
    result = score_observational_module(package, "face", answers)
    assert result.get("scale_direction") != "lower_is_better"
    assert items[0].get("text", items[0]["id"]) in result["strengths"]
    assert items[0].get("text", items[0]["id"]) not in result["attention_items"]
