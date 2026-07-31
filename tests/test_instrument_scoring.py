"""Tests for instrument scoring engines."""

import pytest

from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)
from app.services.instrument_scoring_service import InstrumentScoringService


@pytest.fixture(autouse=True)
def clear_package_cache():
    clear_instrument_content_package_cache()
    yield
    clear_instrument_content_package_cache()


def test_fois_scaled_sum():
    package = get_instrument_content_package("fois")
    scores = InstrumentScoringService.score(package, {"fois_level": 4})
    assert scores["engine"] == "scaled_sum"
    assert scores["total"] == 4
    assert "FOIS" in scores["summary"]


def test_pard_risk_cutoff():
    package = get_instrument_content_package("pard")
    answers = {f"pard_{i:02d}": 1 for i in range(1, 4)}
    scores = InstrumentScoringService.score(package, answers)
    assert scores["total"] == 3
    assert scores["interpretation"] is not None


def test_vb_mapp_domain_mastery():
    package = get_instrument_content_package("vb-mapp")
    items = package.get_items()
    assert len(items) == 170
    assert package.data.get("content_status") == "official-structure"
    assert not any("Registrar conclusão" in (it.get("text") or "") for it in items)
    assert {it["id"] for it in items} >= {"MAND_01", "PLAY_05", "MATH_15"}

    domain_config = package.scoring["domains"]["LING"]
    item_ids = domain_config["item_ids"]
    assert len(item_ids) == 10
    answers = {item_id: 2 for item_id in item_ids[:3]}
    scores = InstrumentScoringService.score(package, answers)
    assert scores["engine"] == "domain_mastery"
    assert "LING" in scores["domains"]
    assert scores["domains"]["LING"] == round((3 / 10) * 100, 1)


def test_ablls_r_domain_mastery():
    package = get_instrument_content_package("ablls-r")
    assert package.version == 2
    assert len(package.get_items()) == 544
    assert len(package.modules) == 25

    domain_a = package.scoring["domains"]["A"]
    assert len(domain_a["item_ids"]) == 19
    answers = {item_id: 2 for item_id in domain_a["item_ids"][:5]}
    scores = InstrumentScoringService.score(package, answers)
    assert scores["engine"] == "domain_mastery"
    assert scores["domains"]["A"] == round((5 / 19) * 100, 1)
    assert "M11" in package.scoring["domains"]["M"]["item_ids"]
    assert "J20" in package.scoring["domains"]["J"]["item_ids"]
    assert "J21" not in {it["id"] for it in package.get_items()}


def test_spm_subform_scoring():
    from app.services.spm_content_package import get_spm_content_package
    from app.services.spm_scoring_service import compute_subform_scores, synthesize_battery_scores

    get_spm_content_package.cache_clear()
    package = get_spm_content_package()
    subforms = package.list_subforms()
    clinical = next(s for s in subforms if s["filler"] == "clinical" and s["item_count"] > 0)
    items = package.get_items(clinical["slug"])[:5]
    answers = {str(item["id"]): 2 for item in items}
    scores = compute_subform_scores(package, clinical["slug"], answers)
    assert scores["subform_slug"] == clinical["slug"]
    assert "overall" in scores
    battery = synthesize_battery_scores([scores])
    assert "summary" in battery
