"""Scoring engines for generic battery instruments (ABFW module kinds)."""

from __future__ import annotations

from typing import Any

from app.services.instrument_content_package import InstrumentContentPackage
from app.services.norms_status import attach_norms_status


def _patient_age_months(birth_date) -> int | None:
    if not birth_date:
        return None
    from datetime import date

    today = date.today()
    months = (today.year - birth_date.year) * 12 + (today.month - birth_date.month)
    if today.day < birth_date.day:
        months -= 1
    return max(0, months)


def _item_answer(answers: dict[str, Any], item_id: str) -> dict[str, Any]:
    raw = answers.get(item_id)
    if isinstance(raw, dict):
        return raw
    if raw is not None:
        return {"value": raw}
    return {}


def score_phonology_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    items = package.get_module_items(module_slug)
    processes_catalog = {p["id"]: p for p in package.data.get("phonological_processes", [])}
    process_counts: dict[str, int] = {}
    correct = 0
    altered = 0
    item_details: list[dict[str, Any]] = []

    for item in items:
        ans = _item_answer(answers, item["id"])
        classification = ans.get("classification", "")
        response = ans.get("response", "")
        processes = ans.get("processes") or []
        if classification == "correct":
            correct += 1
        else:
            altered += 1
        for proc in processes:
            process_counts[proc] = process_counts.get(proc, 0) + 1
        item_details.append(
            {
                "id": item["id"],
                "target": item.get("target"),
                "response": response,
                "classification": classification,
                "processes": processes,
            }
        )

    process_summary = []
    for proc_id, count in sorted(process_counts.items(), key=lambda x: -x[1]):
        meta = processes_catalog.get(proc_id, {})
        expected_age = meta.get("expected_age_months")
        persistent = None
        if patient_age_months is not None and expected_age is not None:
            persistent = patient_age_months > expected_age
        process_summary.append(
            {
                "id": proc_id,
                "label": meta.get("label", proc_id),
                "count": count,
                "expected_age_months": expected_age,
                "persistent": persistent,
            }
        )

    total = len(items)
    pct = round((correct / total) * 100, 1) if total else 0.0
    return {
        "module_kind": "phonology",
        "module_slug": module_slug,
        "correct": correct,
        "altered": altered,
        "total_items": total,
        "percentage": pct,
        "processes": process_summary,
        "items": item_details,
        "summary": f"Fonologia: {correct}/{total} corretas ({pct}%)",
    }


def score_vocabulary_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    items = package.get_module_items(module_slug)
    norms = package.get_norms()
    ref_by_age = norms.get("vocabulary_reference_by_age_months", {})

    categories: dict[str, dict[str, Any]] = {}
    for item in items:
        cat = item.get("category", "geral")
        if cat not in categories:
            categories[cat] = {
                "id": cat,
                "title": item.get("category_title", cat),
                "dvu": 0,
                "nd": 0,
                "ps": 0,
                "other": 0,
                "total": 0,
            }
        categories[cat]["total"] += 1
        ans = _item_answer(answers, item["id"])
        classification = ans.get("classification", "no_response")
        if classification == "dvu":
            categories[cat]["dvu"] += 1
        elif classification == "nd":
            categories[cat]["nd"] += 1
        elif classification == "ps":
            categories[cat]["ps"] += 1
        else:
            categories[cat]["other"] += 1

    ref_key = None
    if patient_age_months is not None and ref_by_age:
        age_keys = sorted(int(k) for k in ref_by_age.keys())
        for age in age_keys:
            if patient_age_months >= age:
                ref_key = str(age)
        if ref_key is None:
            ref_key = str(age_keys[0])

    category_results = []
    total_dvu = 0
    total_items = 0
    for cat in categories.values():
        pct = round((cat["dvu"] / cat["total"]) * 100, 1) if cat["total"] else 0.0
        ref_pct = None
        level = "unknown"
        if ref_key and cat["id"] in ref_by_age.get(ref_key, {}):
            ref_pct = ref_by_age[ref_key][cat["id"]]
            if pct >= ref_pct:
                level = "adequate"
            elif pct >= ref_pct * 0.7:
                level = "attention"
            else:
                level = "altered"
        total_dvu += cat["dvu"]
        total_items += cat["total"]
        category_results.append({**cat, "percentage": pct, "reference_percentage": ref_pct, "level": level})

    overall_pct = round((total_dvu / total_items) * 100, 1) if total_items else 0.0
    return {
        "module_kind": "vocabulary",
        "module_slug": module_slug,
        "dvu": total_dvu,
        "total_items": total_items,
        "percentage": overall_pct,
        "categories": category_results,
        "summary": f"Vocabulário: {total_dvu}/{total_items} DVU ({overall_pct}%)",
    }


def score_fluency_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
) -> dict[str, Any]:
    session = _item_answer(answers, "flu_session")
    duration_seconds = float(session.get("duration_seconds") or 0)
    syllable_count = int(session.get("syllable_count") or 0)
    word_count = int(session.get("word_count") or 0)
    sample_type = str(session.get("sample_type") or "").strip() or None

    stuttering_like_count = 0
    common_count = 0
    disfluency_breakdown: list[dict[str, Any]] = []
    for item in package.get_module_items(module_slug):
        if item.get("stimulus_type") != "counter":
            continue
        ans = _item_answer(answers, item["id"])
        count = int(ans.get("count") or 0)
        category = str(item.get("category") or "common")
        if category == "stuttering":
            stuttering_like_count += count
        else:
            common_count += count
        disfluency_breakdown.append(
            {
                "id": item["id"],
                "label": item.get("text"),
                "count": count,
                "category": category,
            }
        )

    disfluency_total = stuttering_like_count + common_count
    minutes = duration_seconds / 60 if duration_seconds > 0 else 0
    syllables_per_min = round(syllable_count / minutes, 1) if minutes > 0 else 0
    words_per_min = round(word_count / minutes, 1) if minutes > 0 else 0

    def _pct(count: int) -> float:
        return round((count / syllable_count) * 100, 2) if syllable_count > 0 else 0.0

    disfluency_pct = _pct(disfluency_total)
    sld_pct = _pct(stuttering_like_count)
    common_pct = _pct(common_count)

    norms = package.get_norms().get("fluency_reference", {})
    rate_min = float(norms.get("syllables_per_minute_min", 120) or 120)
    rate_max = float(norms.get("syllables_per_minute_max", 200) or 200)
    sld_max = float(norms.get("disfluency_percent_max", 3) or 3)

    if syllables_per_min <= 0:
        rate_status = "unknown"
    elif syllables_per_min < rate_min:
        rate_status = "below"
    elif syllables_per_min > rate_max:
        rate_status = "above"
    else:
        rate_status = "within"

    # Interpretação clínica: tipologias de gagueira (SLD), não o % total
    level = "unknown"
    if syllable_count > 0 and duration_seconds > 0:
        if sld_pct <= sld_max:
            level = "adequate"
        elif sld_pct <= 5:
            level = "attention"
        else:
            level = "altered"

    # Índice só para radar — fluência não entra na média geral da bateria
    if level == "adequate":
        percentage = 100.0 if rate_status == "within" else 85.0
    elif level == "attention":
        percentage = 55.0
    elif level == "altered":
        percentage = max(0.0, round(40.0 - max(0.0, sld_pct - 5) * 5, 1))
    else:
        percentage = 0.0

    sample_label = {
        "reading": "leitura",
        "spontaneous": "fala espontânea",
    }.get(sample_type or "", "amostra")

    return {
        "module_kind": "fluency",
        "module_slug": module_slug,
        "duration_seconds": duration_seconds,
        "syllable_count": syllable_count,
        "word_count": word_count,
        "sample_type": sample_type,
        "syllables_per_minute": syllables_per_min,
        "words_per_minute": words_per_min,
        "rate_status": rate_status,
        "rate_reference": {"min": rate_min, "max": rate_max},
        "disfluency_count": disfluency_total,
        "disfluency_percentage": disfluency_pct,
        "stuttering_like_count": stuttering_like_count,
        "stuttering_like_percentage": sld_pct,
        "common_disfluency_count": common_count,
        "common_disfluency_percentage": common_pct,
        "disfluency_breakdown": disfluency_breakdown,
        "level": level,
        "percentage": percentage,
        "norms_note": "Referência pública aproximada — não substitui normas licenciadas do ABFW.",
        "summary": (
            f"Fluência ({sample_label}): {syllables_per_min} síl/min, "
            f"{sld_pct}% tipicas de gagueira, {disfluency_pct}% total"
        ),
    }


def _observational_band(
    percentage: float, interpretations: list[dict[str, Any]]
) -> dict[str, str]:
    for band in interpretations:
        if "min_percentage" in band:
            min_pct = float(band.get("min_percentage", 0))
            max_pct = float(band.get("max_percentage", 100))
            if min_pct <= percentage <= max_pct:
                level = str(band.get("level", "unknown"))
                return {
                    "level": level,
                    "label": str(band.get("label") or level),
                }
        elif "min" in band and "max" in band:
            min_val = float(band["min"])
            max_val = float(band["max"])
            if min_val <= percentage <= max_val:
                level = str(band.get("level", "unknown"))
                return {
                    "level": level,
                    "label": str(band.get("label") or level),
                }
    return {"level": "unknown", "label": "—"}


def _observational_level(percentage: float, interpretations: list[dict[str, Any]]) -> str:
    return _observational_band(percentage, interpretations)["level"]


def _lookup_norm_table(table: list[dict], value: float, key: str, val_key: str) -> int | None:
    if not table:
        return None
    sorted_rows = sorted(table, key=lambda r: r[key])
    result = None
    for row in sorted_rows:
        if value >= row[key]:
            result = row[val_key]
        else:
            break
    return result


def _apply_domain_norms(
    package: InstrumentContentPackage, domain_id: str, raw_score: int
) -> dict[str, Any]:
    norms = package.get_norms().get("domains", {}).get(domain_id, {})
    standard = _lookup_norm_table(norms.get("raw_to_standard", []), raw_score, "raw", "standard")
    percentile = _lookup_norm_table(
        norms.get("standard_to_percentile", []), standard or 0, "standard", "percentile"
    )
    out: dict[str, Any] = {}
    if standard is not None:
        out["standard_score"] = standard
    if percentile is not None:
        out["percentile"] = percentile
    return out


def _resolve_age_band_id(norms: dict[str, Any], patient_age_months: int | None) -> str | None:
    if patient_age_months is None:
        return None
    for band in norms.get("age_bands", []):
        start = int(band.get("start_months", 0))
        end = int(band.get("end_months", 999))
        if start <= patient_age_months <= end:
            return str(band.get("id"))
    return None


def _adl2_qualitative_max_months(package: InstrumentContentPackage) -> int:
    norms = package.get_norms()
    return int(norms.get("qualitative_max_months") or 35)


def _adl2_band_is_qualitative(
    package: InstrumentContentPackage, age_band_id: str | None
) -> bool:
    if not age_band_id:
        return False
    norms = package.get_norms()
    qualitative = set(norms.get("qualitative_age_bands") or [])
    if age_band_id in qualitative:
        return True
    band_data = (
        norms.get("domains", {})
        .get("LR", {})
        .get("by_age_band", {})
        .get(age_band_id, {})
    )
    return band_data.get("status") == "qualitative"


def _infer_adl2_developmental_age_band(
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    admin_rules: dict[str, Any],
    norms: dict[str, Any],
) -> str | None:
    scored_items, _ = _apply_basal_ceiling_window(items, answers, admin_rules)
    max_end: int | None = None
    for item in scored_items:
        if _is_developmental_pass(_item_answer(answers, item["id"])):
            end = int(item.get("age_end_months") or 0)
            if end and (max_end is None or end > max_end):
                max_end = end
    if max_end is None:
        return None
    return _resolve_age_band_id(norms, max_end)


ADL2_AGE_BAND_LABELS: dict[str, str] = {
    "12-17": "1a0m a 1a5m",
    "18-23": "1a6m a 1a11m",
    "24-29": "2a0m a 2a5m",
    "30-35": "2a6m a 2a11m",
    "36-41": "3a0m a 3a5m",
    "42-47": "3a6m a 3a11m",
    "48-53": "4a0m a 4a5m",
    "54-59": "4a6m a 4a11m",
    "60-65": "5a0m a 5a5m",
    "66-71": "5a6m a 5a11m",
    "72-77": "6a0m a 6a5m",
    "78-83": "6a6m a 6a11m",
}


def _classify_standard_score(
    standard: int, interpretations: list[dict[str, Any]]
) -> dict[str, str]:
    for band in interpretations:
        min_std = int(band.get("min_standard", band.get("min", 0)))
        max_std = int(band.get("max_standard", band.get("max", 999)))
        if min_std <= standard <= max_std:
            return {
                "level": str(band.get("level", "unknown")),
                "label": str(band.get("label", "")),
            }
    return {"level": "unknown", "label": ""}


def _classify_raw_score(raw: int, interpretations: list[dict[str, Any]]) -> dict[str, str]:
    for band in interpretations:
        min_val = int(band.get("min", 0))
        max_val = int(band.get("max", 999))
        if min_val <= raw <= max_val:
            return {
                "level": str(band.get("level", "unknown")),
                "label": str(band.get("label", "")),
            }
    return {"level": "unknown", "label": ""}


def _synthesize_amiofe_totals(
    package: InstrumentContentPackage,
    subform_scores: list[dict[str, Any]],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    total_cfg = package.scoring.get("total_score") or {}
    ref_max = int(total_cfg.get("reference_max") or 103)
    cutoff = int(total_cfg.get("dmo_cutoff") or 89)
    child_months_max = int(total_cfg.get("child_age_months_max") or 144)
    if patient_age_months is not None and patient_age_months <= child_months_max:
        cutoff = int(total_cfg.get("dmo_cutoff_child") or cutoff)
        severity_bands = total_cfg.get("classification_child") or []
    else:
        severity_bands = total_cfg.get("classification_adult") or []

    raw_points = 0
    raw_possible = 0
    module_scores: list[dict[str, Any]] = []
    for score in subform_scores:
        if score.get("module_kind") != "observational":
            continue
        mod = package.get_module_config(score["module_slug"])
        if mod.get("deprecated") or package.is_legacy_module(score["module_slug"]):
            continue
        points = int(score.get("points") or score.get("sum") or 0)
        possible = int(score.get("possible_points") or score.get("max_sum") or 0)
        if not possible:
            possible = int(mod.get("max_sum") or 0)
        raw_points += points
        raw_possible += possible
        module_scores.append(
            {
                "module_slug": score["module_slug"],
                "title": score.get("title", score["module_slug"]),
                "points": points,
                "max_sum": possible,
            }
        )

    etamiofe = round(raw_points / raw_possible * ref_max) if raw_possible else None
    pct = round(raw_points / raw_possible * 100, 1) if raw_possible else 0.0

    categories: dict[str, Any] = {}
    for cat in total_cfg.get("categories") or []:
        cat_id = str(cat["id"])
        cat_modules = set(cat.get("modules") or [])
        cat_points = sum(m["points"] for m in module_scores if m["module_slug"] in cat_modules)
        cat_max = int(cat.get("max_sum") or 0)
        if not cat_max:
            cat_max = sum(m["max_sum"] for m in module_scores if m["module_slug"] in cat_modules)
        cat_pct = round(cat_points / cat_max * 100, 1) if cat_max else 0.0
        categories[cat_id] = {
            "title": cat.get("title", cat_id),
            "points": cat_points,
            "max_sum": cat_max,
            "percentage": cat_pct,
        }

    severity = _classify_raw_score(etamiofe or 0, severity_bands) if etamiofe is not None else {}
    dmo_present = etamiofe is not None and etamiofe < cutoff

    return {
        "raw_total_score": raw_points,
        "raw_max_total": raw_possible,
        "etamiofe_score": etamiofe,
        "etamiofe_max": ref_max,
        "etamiofe_percentage": round(etamiofe / ref_max * 100, 1) if etamiofe is not None else pct,
        "dmo_cutoff": cutoff,
        "dmo_present": dmo_present,
        "severity_level": severity.get("level", "unknown"),
        "severity_label": severity.get("label", ""),
        "categories": categories,
        "modules_scored": len(module_scores),
    }


def _apply_adl2_domain_norms(
    package: InstrumentContentPackage,
    domain_id: str,
    raw_score: int,
    age_band_id: str | None,
) -> dict[str, Any]:
    norms = package.get_norms()
    by_band = norms.get("domains", {}).get(domain_id, {}).get("by_age_band", {})
    band_data = by_band.get(age_band_id or "", {})
    if band_data.get("status") == "qualitative":
        return {
            "raw_score": raw_score,
            "age_band": age_band_id,
            "norm_status": "qualitative",
            "interpretation_mode": "developmental_age_band",
        }
    table = band_data.get("raw_to_standard", [])
    standard = _lookup_norm_table(table, raw_score, "raw", "standard")
    out: dict[str, Any] = {
        "raw_score": raw_score,
        "age_band": age_band_id,
        "norm_status": band_data.get("status", "unknown"),
    }
    if standard is not None:
        out["standard_score"] = standard
        cls = _classify_standard_score(standard, package.scoring.get("interpretations", []))
        out["level"] = cls["level"]
        out["classification_label"] = cls["label"]
    return out


def _compute_adl2_raw_score(
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    admin_rules: dict[str, Any],
) -> int:
    scored_items, _ = _apply_basal_ceiling_window(items, answers, admin_rules)
    last_pass = 0
    fails = 0
    for item in sorted(scored_items, key=lambda row: int(row.get("item_number") or 0)):
        ans = _item_answer(answers, item["id"])
        if _is_developmental_pass(ans):
            last_pass = max(last_pass, int(item.get("item_number") or 0))
        elif _is_developmental_fail(ans):
            fails += 1
    return max(0, last_pass - fails)


def score_adl2_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    mod = package.get_module_config(module_slug)
    if mod.get("scoring_role") == "prerequisite":
        result = score_developmental_module(
            package, module_slug, answers, patient_age_months=patient_age_months
        )
        result["module_kind"] = "adl2_prerequisite"
        return result

    result = score_developmental_module(
        package, module_slug, answers, patient_age_months=patient_age_months
    )
    items = package.get_module_items(module_slug)
    admin_rules = package.scoring.get("administration_rules", {})
    raw = _compute_adl2_raw_score(items, answers, admin_rules)
    result["module_kind"] = "adl2"
    result["raw_score"] = raw
    norms = package.get_norms()
    chrono_band = _resolve_age_band_id(norms, patient_age_months)
    if _adl2_band_is_qualitative(package, chrono_band):
        dev_band = _infer_adl2_developmental_age_band(items, answers, admin_rules, norms)
        if dev_band:
            result["developmental_age_band"] = dev_band
            result["developmental_age_label"] = ADL2_AGE_BAND_LABELS.get(dev_band, dev_band)
        result["norm_status"] = "qualitative"
        result["interpretation_mode"] = "developmental_age_band"
    result["summary"] = (
        f"{mod.get('title', module_slug)}: bruto {raw}, "
        f"{result.get('passes', 0)} acerto(s), {result.get('delay_count', 0)} atraso(s)"
    )
    return result


def _developmental_band(
    delay_count: int, interpretations: list[dict[str, Any]]
) -> dict[str, str]:
    sorted_bands = sorted(interpretations, key=lambda b: int(b.get("max_delays", 999)))
    for band in sorted_bands:
        max_delays = int(band.get("max_delays", 999))
        if delay_count <= max_delays:
            level = str(band.get("level", "unknown"))
            return {"level": level, "label": str(band.get("label") or level)}
    return {"level": "unknown", "label": "—"}


def _developmental_level(delay_count: int, interpretations: list[dict[str, Any]]) -> str:
    return _developmental_band(delay_count, interpretations)["level"]


def _resolve_scale_direction(
    package: InstrumentContentPackage, mod: dict[str, Any]
) -> str | None:
    return mod.get("scale_direction") or package.scoring.get("scale_direction")


def _is_developmental_pass(ans: dict[str, Any]) -> bool:
    response = ans.get("response")
    if response in ("pass", "present", "yes"):
        return True
    value = ans.get("value")
    return value is not None and int(value) >= 1


def _is_developmental_fail(ans: dict[str, Any]) -> bool:
    response = ans.get("response")
    if response in ("fail", "absent", "no"):
        return True
    value = ans.get("value")
    return value is not None and int(value) == 0


def _apply_basal_ceiling_window(
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    admin_rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session = _item_answer(answers, "_session")
    session_meta: dict[str, Any] = {}
    if not session:
        return items, session_meta

    basal_index = session.get("basal_index")
    ceiling_index = session.get("ceiling_index")
    if basal_index is None and ceiling_index is None:
        basal_rule = int(admin_rules.get("basal_rule") or 0)
        ceiling_rule = int(admin_rules.get("ceiling_rule") or 0)
        if not basal_rule and not ceiling_rule:
            return items, session_meta

        passes: list[bool] = []
        for item in items:
            ans = _item_answer(answers, item["id"])
            if ans:
                passes.append(_is_developmental_pass(ans))
            else:
                passes.append(False)

        start_index = session.get("start_index")
        if start_index is None and admin_rules:
            start_index = len(items) // 2
        start_index = int(start_index or 0)

        if basal_rule and basal_index is None:
            streak = 0
            for idx in range(start_index, -1, -1):
                if idx < len(passes) and passes[idx]:
                    streak += 1
                    if streak >= basal_rule:
                        basal_index = idx
                        break
                else:
                    streak = 0
            if basal_index is None:
                basal_index = 0

        if ceiling_rule and ceiling_index is None:
            streak = 0
            for idx in range(start_index, len(items)):
                ans = _item_answer(answers, items[idx]["id"])
                if ans and _is_developmental_fail(ans):
                    streak += 1
                    if streak >= ceiling_rule:
                        ceiling_index = idx
                        break
                else:
                    streak = 0
            if ceiling_index is None:
                ceiling_index = len(items) - 1

    session_meta = {
        "basal_index": basal_index,
        "ceiling_index": ceiling_index,
        "start_index": session.get("start_index"),
    }

    filtered: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        if basal_index is not None and idx < basal_index:
            continue
        if ceiling_index is not None and idx > ceiling_index:
            continue
        filtered.append(item)
    return filtered or items, session_meta


def score_observational_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    mod = package.get_module_config(module_slug)
    items = package.get_module_items(module_slug)
    module_kind = mod.get("module_kind", "observational")
    if module_kind == "qualitative":
        notes: list[dict[str, Any]] = []
        for item in items:
            ans = _item_answer(answers, item["id"])
            text = ans.get("text") or ans.get("notes") or ""
            if text:
                notes.append({"id": item["id"], "text": item.get("text"), "notes": text})
        return {
            "module_kind": "qualitative",
            "module_slug": module_slug,
            "domain": mod.get("domain", module_slug),
            "notes": notes,
            "summary": f"{mod.get('title', module_slug)}: {len(notes)} observação(ões) registrada(s)",
        }

    scale = mod.get("scale") or package.scale
    max_value = max((int(s["value"]) for s in scale), default=2)
    interpretations = package.scoring.get("interpretations", [])
    zero_label = next((str(s.get("label", "")) for s in scale if int(s["value"]) == 0), "")
    zero_is_not_observed = mod.get("zero_is_not_observed")
    if zero_is_not_observed is None:
        zero_is_not_observed = "não observado" in zero_label.lower() or "not observed" in zero_label.lower()

    item_details: list[dict[str, Any]] = []
    not_observed = 0
    unanswered = 0
    points = 0
    possible_points = 0
    strengths: list[str] = []
    attention_items: list[str] = []

    for item in items:
        input_type = item.get("input_type") or mod.get("input_type", "scale")
        ans = _item_answer(answers, item["id"])

        if input_type == "checklist":
            selected = ans.get("selected") or []
            if not selected and not ans.get("notes"):
                unanswered += 1
                item_details.append(
                    {
                        "id": item["id"],
                        "text": item.get("text"),
                        "input_type": "checklist",
                        "selected": [],
                        "status": "unanswered",
                        "notes": ans.get("notes", ""),
                    }
                )
                continue
            total_options = len(item.get("options") or [])
            if total_options:
                present_count = len(selected)
                possible_points += max_value
                item_points = round((present_count / total_options) * max_value)
                points += item_points
                if present_count >= total_options * 0.7:
                    strengths.append(item.get("text", item["id"]))
                elif present_count <= total_options * 0.3:
                    attention_items.append(item.get("text", item["id"]))
            item_details.append(
                {
                    "id": item["id"],
                    "text": item.get("text"),
                    "input_type": "checklist",
                    "selected": selected,
                    "status": "scored",
                    "notes": ans.get("notes", ""),
                }
            )
            continue

        if input_type == "text":
            text = ans.get("text") or ans.get("notes") or ""
            if text:
                item_details.append(
                    {
                        "id": item["id"],
                        "text": item.get("text"),
                        "input_type": "text",
                        "notes": text,
                        "status": "answered",
                    }
                )
            continue

        value = ans.get("value")
        if value is None:
            unanswered += 1
            item_details.append(
                {
                    "id": item["id"],
                    "text": item.get("text"),
                    "value": None,
                    "status": "unanswered",
                    "notes": ans.get("notes", ""),
                }
            )
            continue

        value = int(value)
        if value == 0 and zero_is_not_observed:
            not_observed += 1
            item_details.append(
                {
                    "id": item["id"],
                    "text": item.get("text"),
                    "value": 0,
                    "status": "not_observed",
                    "notes": ans.get("notes", ""),
                }
            )
            continue

        possible_points += max_value
        points += value
        item_details.append(
            {
                "id": item["id"],
                "text": item.get("text"),
                "value": value,
                "status": "scored",
                "notes": ans.get("notes", ""),
            }
        )
        if value == max_value:
            strengths.append(item.get("text", item["id"]))
        elif value == 1:
            attention_items.append(item.get("text", item["id"]))

    percentage = round((points / possible_points) * 100, 1) if possible_points else 0.0
    scale_direction = _resolve_scale_direction(package, mod)
    if scale_direction == "lower_is_better":
        percentage = round(100 - percentage, 1)

    interpretations = package.scoring.get("interpretations", [])
    if interpretations and "min_percentage" not in interpretations[0] and "min" in interpretations[0]:
        max_band = max(float(b.get("max", 0)) for b in interpretations)
        band = _observational_band(
            percentage if max_band <= 100 else float(points),
            interpretations,
        )
    else:
        band = _observational_band(percentage, interpretations)
    level = band["level"]
    level_label = band["label"]

    return {
        "module_kind": "observational",
        "module_slug": module_slug,
        "domain": mod.get("domain", module_slug),
        "title": mod.get("title", module_slug),
        "percentage": percentage,
        "level": level,
        "level_label": level_label,
        "points": points,
        "possible_points": possible_points,
        "sum": points,
        "max_sum": possible_points,
        "scale_direction": scale_direction,
        "not_observed": not_observed,
        "unanswered": unanswered,
        "strengths": strengths,
        "attention_items": attention_items,
        "items": item_details,
        "summary": f"{mod.get('title', module_slug)}: {percentage}% ({level_label})",
    }


def score_developmental_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    mod = package.get_module_config(module_slug)
    items = package.get_module_items(module_slug)
    admin_rules = package.scoring.get("administration_rules", {})
    scored_items, session_meta = _apply_basal_ceiling_window(items, answers, admin_rules)

    item_details: list[dict[str, Any]] = []
    delays: list[dict[str, Any]] = []
    passes = 0
    fails = 0
    unanswered = 0

    for item in scored_items:
        ans = _item_answer(answers, item["id"])
        if not ans:
            unanswered += 1
            item_details.append(
                {
                    "id": item["id"],
                    "text": item.get("text"),
                    "status": "unanswered",
                    "age_start_months": item.get("age_start_months"),
                    "age_end_months": item.get("age_end_months"),
                }
            )
            continue

        passed = _is_developmental_pass(ans)
        failed = _is_developmental_fail(ans)
        response = ans.get("response")
        if passed:
            passes += 1
            status = "pass"
        elif failed:
            fails += 1
            status = "fail"
        else:
            status = "unset"

        age_end = item.get("age_end_months")
        is_delayed = False
        if patient_age_months is not None and age_end is not None and failed:
            if patient_age_months > int(age_end):
                is_delayed = True
                delays.append(
                    {
                        "id": item["id"],
                        "text": item.get("text"),
                        "age_end_months": age_end,
                        "patient_age_months": patient_age_months,
                    }
                )

        item_details.append(
            {
                "id": item["id"],
                "text": item.get("text"),
                "response": response,
                "value": ans.get("value"),
                "status": status,
                "delayed": is_delayed,
                "age_start_months": item.get("age_start_months"),
                "age_end_months": age_end,
                "notes": ans.get("notes", ""),
            }
        )

    delay_count = len(delays)
    interpretations = package.scoring.get("interpretations", [])
    band = _developmental_band(delay_count, interpretations)
    level = band["level"]
    level_label = band["label"]
    total_scored = passes + fails
    pass_rate = round((passes / total_scored) * 100, 1) if total_scored else 0.0

    session = _item_answer(answers, "_session")
    if session and not session_meta:
        session_meta = {
            "basal_index": session.get("basal_index"),
            "ceiling_index": session.get("ceiling_index"),
            "start_index": session.get("start_index"),
        }

    return {
        "module_kind": "developmental",
        "module_slug": module_slug,
        "domain": mod.get("domain", module_slug),
        "title": mod.get("title", module_slug),
        "passes": passes,
        "fails": fails,
        "delay_count": delay_count,
        "delays": delays,
        "level": level,
        "level_label": level_label,
        "percentage": pass_rate,
        "unanswered": unanswered,
        "items": item_details,
        "session": session_meta,
        "summary": (
            f"{mod.get('title', module_slug)}: {passes}/{total_scored} passou, "
            f"{delay_count} atraso(s) ({level_label})"
        ),
    }


def score_pragmatics_module(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
) -> dict[str, Any]:
    items = package.get_module_items(module_slug)
    counts = {"adequate": 0, "attention": 0, "altered": 0, "unset": 0}
    checklist: list[dict[str, Any]] = []

    for item in items:
        ans = _item_answer(answers, item["id"])
        level = ans.get("classification") or ans.get("level") or "unset"
        if level not in counts:
            level = "unset"
        counts[level] += 1
        checklist.append(
            {
                "id": item["id"],
                "text": item.get("text"),
                "category": item.get("category"),
                "level": level,
                "notes": ans.get("notes", ""),
            }
        )

    total = len(items)
    adequate_pct = round((counts["adequate"] / total) * 100, 1) if total else 0.0
    return {
        "module_kind": "pragmatics",
        "module_slug": module_slug,
        "counts": counts,
        "checklist": checklist,
        "percentage": adequate_pct,
        "summary": f"Pragmática: {counts['adequate']}/{total} itens adequados",
    }


def score_battery_subform(
    package: InstrumentContentPackage,
    module_slug: str,
    answers: dict[str, Any],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    mod = package.get_module_config(module_slug)
    kind = mod.get("module_kind", "generic")
    dispatch = {
        "phonology": score_phonology_module,
        "vocabulary": score_vocabulary_module,
        "fluency": score_fluency_module,
        "pragmatics": score_pragmatics_module,
        "observational": score_observational_module,
        "qualitative": score_observational_module,
        "developmental": score_developmental_module,
    }
    handler = dispatch.get(kind)
    if not handler:
        total = len(answers)
        return {"module_kind": kind, "module_slug": module_slug, "total_items": total, "summary": "Concluído"}
    if package.scoring.get("engine") == "adl2" and kind == "developmental":
        return score_adl2_module(
            package, module_slug, answers, patient_age_months=patient_age_months
        )
    if kind in ("phonology", "vocabulary"):
        return handler(package, module_slug, answers, patient_age_months=patient_age_months)
    if kind in ("observational", "qualitative", "developmental"):
        return handler(package, module_slug, answers, patient_age_months=patient_age_months)
    return handler(package, module_slug, answers)


def synthesize_battery_scores(
    package: InstrumentContentPackage,
    subform_scores: list[dict[str, Any]],
    *,
    patient_age_months: int | None = None,
) -> dict[str, Any]:
    domains: dict[str, Any] = {}
    percentages: list[float] = []
    critical_items: list[dict[str, Any]] = []

    domain_groups: dict[str, list[dict[str, Any]]] = {}
    for score in subform_scores:
        mod = package.get_module_config(score["module_slug"])
        domain_id = mod.get("domain", score["module_slug"])
        domain_groups.setdefault(domain_id, []).append(score)

    for domain_id, group in domain_groups.items():
        if len(group) == 1:
            entry = group[0].copy()
            if package.scoring.get("engine") == "observational_domains" and "level" not in entry:
                entry["level"] = _observational_level(
                    float(entry.get("percentage") or 0),
                    package.scoring.get("interpretations", []),
                )
            domain_meta = next((d for d in package.domains if d.get("id") == domain_id), None)
            if domain_meta:
                entry["title"] = domain_meta.get("title", domain_id)
            domains[domain_id] = entry
        else:
            pcts = [float(s["percentage"]) for s in group if "percentage" in s]
            merged_pct = round(sum(pcts) / len(pcts), 1) if pcts else 0.0
            merged: dict[str, Any] = {
                "module_kind": group[0].get("module_kind"),
                "module_slug": domain_id,
                "title": next(
                    (d.get("title", domain_id) for d in package.domains if d.get("id") == domain_id),
                    domain_id,
                ),
                "submodules": group,
                "percentage": merged_pct,
                "summary": " · ".join(s.get("summary", "") for s in group),
            }
            if package.scoring.get("engine") == "observational_domains":
                merged["level"] = _observational_level(
                    merged_pct, package.scoring.get("interpretations", [])
                )
            domains[domain_id] = merged

    for score in subform_scores:
        mod = package.get_module_config(score["module_slug"])
        domain_id = mod.get("domain", score["module_slug"])
        domain_score = domains.get(domain_id, score)
        # Fluência usa percentage só para radar; não distorce a média de DVU/fonologia
        if "percentage" in score and score.get("module_kind") != "fluency":
            percentages.append(float(score["percentage"]))

        if score.get("module_kind") == "fluency":
            level = score.get("level")
            if level in ("attention", "altered"):
                critical_items.append(
                    {
                        "type": "fluency",
                        "label": "Fluência — tipologias de gagueira",
                        "detail": (
                            f"{score.get('stuttering_like_percentage')}% SLD · "
                            f"{score.get('disfluency_percentage')}% total · "
                            f"{score.get('syllables_per_minute')} síl/min "
                            f"(ref. pública)"
                        ),
                    }
                )
            rate_status = score.get("rate_status")
            if rate_status in ("below", "above"):
                critical_items.append(
                    {
                        "type": "fluency_rate",
                        "label": "Fluência — velocidade de fala",
                        "detail": (
                            f"{score.get('syllables_per_minute')} síl/min "
                            f"({'abaixo' if rate_status == 'below' else 'acima'} "
                            f"da faixa {score.get('rate_reference', {}).get('min')}-"
                            f"{score.get('rate_reference', {}).get('max')})"
                        ),
                    }
                )

        if score.get("module_kind") == "vocabulary":
            vocab = domain_score if domain_score.get("module_kind") == "vocabulary" else score
            for cat in vocab.get("categories", []):
                if cat.get("level") == "altered":
                    critical_items.append(
                        {
                            "type": "category",
                            "label": cat.get("title"),
                            "detail": f"{cat.get('percentage')}% DVU (ref. {cat.get('reference_percentage')}%)",
                        }
                    )
        if score.get("module_kind") == "phonology":
            phon = score
            for proc in phon.get("processes", []):
                if proc.get("persistent"):
                    critical_items.append(
                        {
                            "type": "process",
                            "label": proc.get("label"),
                            "detail": f"{proc.get('count')} ocorrências — persistente para idade",
                        }
                    )
        if score.get("module_kind") in ("developmental", "adl2"):
            for delay in score.get("delays") or []:
                critical_items.append(
                    {
                        "type": "developmental_delay",
                        "label": delay.get("text"),
                        "detail": (
                            f"Atraso para idade (limite {delay.get('age_end_months')} meses, "
                            f"paciente {delay.get('patient_age_months')} meses)"
                        ),
                    }
                )

    overall = round(sum(percentages) / len(percentages), 1) if percentages else 0.0
    engine = package.scoring.get("engine", "battery_module_kind")
    title = package.instrument_title
    default_interpretation = (
        f"Desempenho agregado de {overall}% nos módulos aplicados. "
        "Correlacionar achados com avaliação clínica completa."
    )
    interpretation = default_interpretation

    observational_meta: dict[str, Any] = {}
    if engine == "observational_domains":
        all_strengths: list[str] = []
        all_attention: list[str] = []
        not_observed_total = 0
        unanswered_total = 0
        for score in subform_scores:
            if score.get("module_kind") == "observational":
                all_strengths.extend(score.get("strengths") or [])
                all_attention.extend(score.get("attention_items") or [])
                not_observed_total += int(score.get("not_observed") or 0)
                unanswered_total += int(score.get("unanswered") or 0)
        observational_meta = {
            "strengths": all_strengths[:10],
            "attention_items": all_attention[:10],
            "not_observed_count": not_observed_total,
            "unanswered_count": unanswered_total,
        }
        if package.scoring.get("total_score"):
            amiofe_meta = _synthesize_amiofe_totals(
                package, subform_scores, patient_age_months=patient_age_months
            )
            observational_meta.update(amiofe_meta)
            if amiofe_meta.get("etamiofe_score") is not None:
                interpretation = (
                    f"ETAMIOFE {amiofe_meta['etamiofe_score']}/{amiofe_meta['etamiofe_max']} — "
                    f"{amiofe_meta.get('severity_label', '—')}. "
                    f"{'DMO presente' if amiofe_meta.get('dmo_present') else 'Sem DMO'} "
                    f"(corte ≥ {amiofe_meta.get('dmo_cutoff')})."
                )
                overall = float(amiofe_meta.get("etamiofe_percentage") or overall)

    developmental_meta: dict[str, Any] = {}
    if engine == "adl2":
        norms = package.get_norms()
        age_band = _resolve_age_band_id(norms, patient_age_months)
        norms_applied = False
        lr_std: int | None = None
        le_std: int | None = None
        for domain_id, entry in domains.items():
            if not isinstance(entry, dict) or domain_id == "PRE":
                continue
            raw = entry.get("raw_score")
            if raw is None:
                continue
            norm_fields = _apply_adl2_domain_norms(package, domain_id, int(raw), age_band)
            if norm_fields.get("standard_score") is not None:
                norms_applied = True
            entry.update(norm_fields)
            if domain_id == "LR":
                lr_std = norm_fields.get("standard_score")
            elif domain_id == "LE":
                le_std = norm_fields.get("standard_score")

        total_delays = 0
        delay_domains: list[dict[str, Any]] = []
        domain_levels: dict[str, str] = {}
        for score in subform_scores:
            if score.get("module_kind") not in ("adl2", "developmental"):
                continue
            mod = package.get_module_config(score["module_slug"])
            if mod.get("scoring_role") == "prerequisite":
                continue
            delay_count = int(score.get("delay_count") or 0)
            total_delays += delay_count
            domain_id = str(score.get("domain") or score["module_slug"])
            domain_levels[domain_id] = str(
                domains.get(domain_id, {}).get("level") or score.get("level", "unknown")
            )
            if delay_count > 0:
                delay_domains.append(
                    {
                        "domain": domain_id,
                        "title": score.get("title", domain_id),
                        "delay_count": delay_count,
                        "level": domain_levels[domain_id],
                    }
                )

        developmental_meta = {
            "total_delays": total_delays,
            "delay_domains": delay_domains,
            "domain_levels": domain_levels,
            "age_band": age_band,
            "patient_age_months": patient_age_months,
        }
        if norms_applied:
            developmental_meta["norms_applied"] = True
        if lr_std is not None and le_std is not None and age_band:
            global_band = norms.get("global_language", {}).get("by_age_band", {}).get(age_band, {})
            global_ep = _lookup_norm_table(
                global_band.get("sum_to_standard", []),
                lr_std + le_std,
                "sum",
                "standard",
            )
            if global_ep is not None:
                developmental_meta["global_standard_score"] = global_ep
                developmental_meta["composite_standard_score"] = global_ep
                cls = _classify_standard_score(
                    int(global_ep), package.scoring.get("interpretations", [])
                )
                developmental_meta["global_level"] = cls["level"]
                developmental_meta["global_classification_label"] = cls["label"]
        if domain_levels:
            lr_entry = domains.get("LR") if isinstance(domains.get("LR"), dict) else {}
            le_entry = domains.get("LE") if isinstance(domains.get("LE"), dict) else {}
            qualitative_mode = _adl2_band_is_qualitative(package, age_band)
            if qualitative_mode:
                lr_label = lr_entry.get("developmental_age_label") or lr_entry.get("developmental_age_band")
                le_label = le_entry.get("developmental_age_label") or le_entry.get("developmental_age_band")
                interpretation = (
                    f"ADL 2 (1a0m–2a11m): interpretação qualitativa conforme manual. "
                    f"Faixa de desenvolvimento LC: {lr_label or '—'}; LE: {le_label or '—'}. "
                    f"{total_delays} atraso(s) por idade nos itens aplicados."
                )
                developmental_meta["interpretation_mode"] = "qualitative"
            else:
                interpretation = (
                    f"ADL 2: EP global {developmental_meta.get('global_standard_score', '—')} "
                    f"({developmental_meta.get('global_classification_label', 'sem norma')}). "
                    f"{total_delays} atraso(s) por idade nos itens aplicados."
                )
        else:
            interpretation = default_interpretation
    elif engine == "developmental_screening":
        standard_scores: list[int] = []
        percentiles: list[int] = []
        norms_applied = False
        for domain_id, entry in domains.items():
            if not isinstance(entry, dict):
                continue
            raw = entry.get("raw_score")
            if raw is None:
                raw = entry.get("passes")
            if raw is not None:
                norm_fields = _apply_domain_norms(package, domain_id, int(raw))
                if norm_fields:
                    entry.update(norm_fields)
                    norms_applied = True
                    if "standard_score" in norm_fields:
                        standard_scores.append(norm_fields["standard_score"])
                    if "percentile" in norm_fields:
                        percentiles.append(norm_fields["percentile"])

        total_delays = 0
        delay_domains: list[dict[str, Any]] = []
        domain_levels: dict[str, str] = {}
        for score in subform_scores:
            if score.get("module_kind") != "developmental":
                continue
            delay_count = int(score.get("delay_count") or 0)
            total_delays += delay_count
            domain_id = str(score.get("domain") or score["module_slug"])
            domain_levels[domain_id] = str(score.get("level", "unknown"))
            if delay_count > 0:
                delay_domains.append(
                    {
                        "domain": domain_id,
                        "title": score.get("title", domain_id),
                        "delay_count": delay_count,
                        "level": score.get("level"),
                    }
                )
        developmental_meta = {
            "total_delays": total_delays,
            "delay_domains": delay_domains,
            "domain_levels": domain_levels,
        }
        if norms_applied:
            developmental_meta["norms_applied"] = True
        if standard_scores:
            developmental_meta["composite_standard_score"] = round(
                sum(standard_scores) / len(standard_scores)
            )
        if percentiles:
            developmental_meta["composite_percentile"] = round(
                sum(percentiles) / len(percentiles)
            )
        if domain_levels:
            altered_domains = sum(
                1 for lvl in domain_levels.values() if lvl in ("delay", "caution", "altered")
            )
            interpretation = (
                f"Triagem do desenvolvimento: {total_delays} atraso(s) detectado(s) "
                f"em {altered_domains} domínio(s). Correlacionar com avaliação clínica completa."
            )

    return attach_norms_status(
        {
            "engine": engine,
            "domains": domains,
            "subforms": subform_scores,
            "total": overall,
            "percentage": overall,
            "critical_items": critical_items,
            **observational_meta,
            **developmental_meta,
            "summary": f"{title} — desempenho geral {overall}%",
            "interpretation": interpretation,
        },
        package,
    )


def battery_scores_to_fields(scores: dict[str, Any]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for slug, domain_score in (scores.get("domains") or {}).items():
        if isinstance(domain_score, dict):
            fields.append({"label": slug, "value": domain_score.get("summary", str(domain_score))})
    fields.append({"label": "Geral", "value": f"{scores.get('percentage', 0)}%"})
    return fields
