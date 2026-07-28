"""Tests for AMIOFE-E observational package."""

from app.services.battery_scoring_service import (
    score_observational_module,
    synthesize_battery_scores,
)
from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)


def _clear():
    clear_instrument_content_package_cache()


def test_amiofe_legacy_module_resolves():
    _clear()
    package = get_instrument_content_package("amiofe")
    mod = package.get_module_config("degluticao")
    assert mod.get("deprecated") is True
    items = package.get_module_items("degluticao")
    assert items[0]["id"] == "deg_reflexo"
    assert package.is_legacy_module("degluticao")


def test_amiofe_package_loads():
    _clear()
    package = get_instrument_content_package("amiofe")
    assert package.slug == "amiofe"
    assert "AMIOFE-E" in package.instrument_title
    assert len(package.modules) == 21
    assert "degluticao" in (package.data.get("legacy_modules") or {})
    face = package.get_module_items("face")
    assert len(face) == 3
    assert face[0]["id"] == "face_simetria"


def test_amiofe_face_scoring():
    _clear()
    package = get_instrument_content_package("amiofe")
    answers = {
        "face_simetria": {"value": 4},
        "face_proporcao_tercos": {"value": 3},
        "face_sulco_nasolabial": {"value": 4},
    }
    result = score_observational_module(package, "face", answers)
    assert result["points"] == 11
    assert result["possible_points"] == 12
    assert result["percentage"] == 91.7


def test_amiofe_mobilidade_scale_6():
    _clear()
    package = get_instrument_content_package("amiofe")
    answers = {f"ml_{suffix}": {"value": 6} for suffix in (
        "protrusao", "retracao", "lateral_d", "lateral_e", "elevar", "abaixar"
    )}
    result = score_observational_module(package, "mobilidade-lingua", answers)
    assert result["points"] == 36
    assert result["level"] == "expected"
    assert result["level_label"]
    assert result["level_label"] != "expected"
    assert "(" not in result["summary"] or "expected" not in result["summary"]


def test_amiofe_synthesize_etamiofe():
    _clear()
    package = get_instrument_content_package("amiofe")
    perfect_face = {
        "face_simetria": {"value": 4},
        "face_proporcao_tercos": {"value": 4},
        "face_sulco_nasolabial": {"value": 4},
    }
    perfect_resp = {"resp_modo": {"value": 4}}
    subforms = [
        score_observational_module(package, "face", perfect_face),
        score_observational_module(package, "respiracao", perfect_resp),
    ]
    synth = synthesize_battery_scores(package, subforms, patient_age_months=120)
    assert synth.get("etamiofe_score") is not None
    assert synth["etamiofe_max"] == 103
    assert synth.get("dmo_present") is False
    assert synth.get("severity_level") == "expected"
    assert "aparencia" in synth.get("categories", {})
