"""Fidelidade textual FOIS (Crary) + DOSS (O'Neil): 7 níveis, sem placeholders genéricos."""

from __future__ import annotations

import re

from app.services.instrument_content_package import (
    _resolve_manifest_path,
    clear_instrument_content_package_cache,
    get_instrument_content_package,
)

_GENERIC_LEVEL_ONLY = re.compile(r"^nível\s*\d+\s*$", re.IGNORECASE)


def _assert_seven_level_scale(slug: str) -> list[dict]:
    clear_instrument_content_package_cache()
    path = _resolve_manifest_path(slug)
    assert "instrument_samples" not in str(path).replace("\\", "/")

    package = get_instrument_content_package(slug)
    scale = package.scale
    assert len(scale) == 7
    assert [int(entry["value"]) for entry in scale] == list(range(1, 8))
    return scale


def test_fois_package_has_seven_public_levels():
    scale = _assert_seven_level_scale("fois")
    labels = [str(entry["label"]) for entry in scale]

    assert "nada por via oral" in labels[0].lower()
    assert "sonda" in labels[1].lower()
    assert "sonda" in labels[2].lower()
    assert "única consistência" in labels[3].lower() or "unica consistencia" in labels[3].lower()
    assert "preparo especial" in labels[4].lower() or "compensa" in labels[4].lower()
    assert "limita" in labels[5].lower()
    assert "sem restrições" in labels[6].lower() or "sem restricoes" in labels[6].lower()

    joined = " ".join(labels).lower()
    assert "tracostomia" not in joined  # typo legado
    for label in labels:
        assert not _GENERIC_LEVEL_ONLY.match(label.strip())
        assert len(label.strip()) > 12


def test_doss_package_has_clinical_descriptors_not_generic_levels():
    scale = _assert_seven_level_scale("doss")
    labels = [str(entry["label"]) for entry in scale]

    assert "npo" in labels[0].lower() or "nada por via oral" in labels[0].lower()
    assert "moderadamente severa" in labels[1].lower()
    assert "moderada" in labels[2].lower()
    assert "leve a moderada" in labels[3].lower() or "leve–moderada" in labels[3].lower()
    assert "leve" in labels[4].lower()
    assert "funcion" in labels[5].lower()
    assert "normal" in labels[6].lower()

    for label in labels:
        assert not _GENERIC_LEVEL_ONLY.match(label.strip())
        # Não aceitar só "Nível N" (com ou sem hífen vazio)
        stripped = re.sub(r"^nível\s*\d+\s*[—\-:]?\s*", "", label.strip(), flags=re.IGNORECASE)
        assert len(stripped) > 20
        assert "disfagia" in label.lower() or "normal" in label.lower() or "funcion" in label.lower()
