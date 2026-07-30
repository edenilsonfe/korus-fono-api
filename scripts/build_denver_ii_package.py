#!/usr/bin/env python3
"""Generate Denver II package items, manifest and norms stub.

Source: UNIFESP/EPM Portuguese translation of Denver II (Pedromônico et al., 1999),
based on the Frankenburg & Dodds Denver II (1990/1992) structure. Ages embedded per
item approximate the 25%/90% percentile bars in whole months — a reference for
triagem, not a substitute for the licensed publisher kit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "app" / "data" / "denver-ii"
ITEMS_DIR = DATA / "items"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from denver_ii_item_catalog import (  # noqa: E402
    FM_ITEMS,
    LA_ITEMS,
    MG_ITEMS,
    PS_ITEMS,
)

SCALE = [
    {"value": 0, "label": "Falha / Ausente"},
    {"value": 1, "label": "Passa / Presente"},
]

MODULES: list[dict] = [
    {
        "slug": "pessoal-social",
        "title": "Pessoal-Social",
        "domain": "PS",
        "file": "pessoal-social.json",
        "items": PS_ITEMS,
    },
    {
        "slug": "motor-fino",
        "title": "Motor Fino",
        "domain": "FM",
        "file": "motor-fino.json",
        "items": FM_ITEMS,
    },
    {
        "slug": "linguagem",
        "title": "Linguagem",
        "domain": "LA",
        "file": "linguagem.json",
        "items": LA_ITEMS,
    },
    {
        "slug": "motor-grosso",
        "title": "Motor Grosso",
        "domain": "MG",
        "file": "motor-grosso.json",
        "items": MG_ITEMS,
    },
]

CONTENT_NOTE = (
    "Itens traduzidos e adaptados por Pedromônico MRM et al. (1999), Escola "
    "Paulista de Medicina — UNIFESP, a partir do Denver II (Frankenburg & Dodds, "
    "1990/1992). Estrutura e enunciados fiéis à tradução brasileira; uso comercial "
    "do instrumento requer autorização do detentor dos direitos (Denver "
    "Developmental Materials). Cobertura etária: 0 a 6 anos."
)


def build_manifest() -> dict:
    modules: dict[str, dict] = {}
    subtests: list[dict] = []
    domains: list[dict] = []

    for mod in MODULES:
        slug = mod["slug"]
        item_count = len(mod["items"])
        modules[slug] = {
            "id": slug,
            "title": mod["title"],
            "domain": mod["domain"],
            "module_kind": "developmental",
            "items_file": f"items/{mod['file']}",
            "item_count": item_count,
            "filler": "clinician",
        }
        subtests.append({"id": slug, "title": mod["title"], "item_count": item_count})
        domains.append({"id": mod["domain"], "title": mod["title"]})

    return {
        "version": 3,
        "package_id": "denver-ii-br-v1",
        "instrument_slug": "denver-ii",
        "instrument_title": "Denver II — Triagem do Desenvolvimento",
        "publisher": "Pedromônico et al. (1999) — tradução UNIFESP/EPM",
        "license_ref": "UNIFESP-TRANSLATION-1999",
        "content_status": "official-structure",
        "content_note": CONTENT_NOTE,
        "norms_region": "BR",
        "norms_file": "norms-br.json",
        "age_coverage_months": {"min": 0, "max": 72},
        "archetype": "observational",
        "supports_multi_session": True,
        "scale": SCALE,
        "domains": domains,
        "modules": modules,
        "subtests": subtests,
        "scoring": {
            "engine": "developmental_screening",
            "administration_rules": {
                "basal_rule": 3,
                "ceiling_rule": 3,
            },
            "interpretations": [
                {"level": "expected", "max_delays": 0, "label": "Desenvolvimento esperado"},
                {"level": "caution", "max_delays": 2, "label": "Atenção — possível atraso"},
                {"level": "delay", "max_delays": 999, "label": "Atraso significativo"},
            ],
        },
        "report": {
            "template_id": "denver-ii-br-v1",
            "sections": ["identificacao", "resultados", "interpretacao", "recomendacoes"],
        },
        "informant_forms": [],
        "requires_competency_ack": False,
    }


def build_norms() -> dict:
    return {
        "version": 2,
        "region": "BR",
        "status": "partial",
        "source": "Pedromônico et al. (1999) — tradução UNIFESP/EPM do Denver II",
        "note": (
            "Percentis 25%/75%/90% de idade aproximados estão embutidos em cada "
            "item do pacote (age_start_months ~25%, age_end_months ~90%), como "
            "referência de triagem. Tabelas normativas brasileiras completas "
            "(incluindo 75%) e barras oficiais de fidelidade psicométrica ainda "
            "dependem da publicação de um kit licenciado do editor."
        ),
        "domains": {},
        "age_bands": [],
    }


def main() -> None:
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)

    for mod in MODULES:
        path = ITEMS_DIR / mod["file"]
        path.write_text(
            json.dumps(mod["items"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {path.relative_to(ROOT)}: {len(mod['items'])} itens")

    manifest = build_manifest()
    manifest_path = DATA / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    total_items = sum(len(mod["items"]) for mod in MODULES)
    print(f"Wrote {manifest_path.relative_to(ROOT)}: {len(manifest['modules'])} módulos, {total_items} itens")

    norms = build_norms()
    norms_path = DATA / "norms-br.json"
    norms_path.write_text(
        json.dumps(norms, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {norms_path.relative_to(ROOT)}: status={norms['status']}")


if __name__ == "__main__":
    main()
