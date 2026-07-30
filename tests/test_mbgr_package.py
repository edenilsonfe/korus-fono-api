"""Sanity checks for MBGR public-structure package (fidelity audit)."""

from app.services.instrument_content_package import (
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)

EXPECTED_MODULES = {
    "historia": 3,
    "face": 4,
    "labios": 4,
    "lingua": 4,
    "mobilidade": 4,
    "respiracao": 3,
    "mastigacao": 4,
    "degluticao": 4,
    "fala": 4,
}

EXPECTED_SCALE = [
    (0, "Adequado"),
    (1, "Alteração leve"),
    (2, "Alteração severa"),
    (3, "Não realiza"),
]


def test_mbgr_module_and_item_counts():
    clear_instrument_content_package_cache()
    package = get_instrument_content_package("mbgr")
    assert package is not None
    assert package.scoring.get("scale_direction") == "lower_is_better"
    assert package.scoring.get("engine") == "observational_domains"

    total = 0
    for module_id, expected_count in EXPECTED_MODULES.items():
        items = package.get_module_items(module_id)
        assert len(items) == expected_count, module_id
        mod = package.get_module_config(module_id)
        if module_id != "historia":
            assert mod.get("scale_direction") == "lower_is_better"
        total += len(items)
    assert total == 34


def test_mbgr_scale_labels_are_severity_not_frequency():
    clear_instrument_content_package_cache()
    package = get_instrument_content_package("mbgr")
    scale = [(int(s["value"]), s["label"]) for s in package.scale]
    assert scale == EXPECTED_SCALE
    labels = " ".join(label for _, label in scale).lower()
    assert "ocasionalmente" not in labels
    assert "frequentemente" not in labels
