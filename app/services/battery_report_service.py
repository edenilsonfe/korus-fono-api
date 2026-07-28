"""PDF report export for generic battery assessments."""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.schemas.battery import BatteryResponse
from app.services.instrument_content_package import InstrumentContentPackage


def export_battery_pdf(battery: BatteryResponse, package: InstrumentContentPackage) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph("KorusFono", styles["Title"]),
        Paragraph(package.instrument_title, styles["Heading1"]),
        Spacer(1, 12),
        Paragraph(f"Paciente: {battery.patient_name or battery.patient_id}", styles["Normal"]),
        Paragraph(f"Data: {battery.created_at.date().isoformat()}", styles["Normal"]),
    ]
    if battery.duration_minutes:
        story.append(Paragraph(f"Duração: {battery.duration_minutes} minutos", styles["Normal"]))
    story.append(Spacer(1, 16))

    if package.scoring.get("engine") == "observational_domains":
        story.append(Paragraph("Objetivo", styles["Heading2"]))
        story.append(
            Paragraph(
                "Avaliação observacional de comunicação, linguagem e aspectos cognitivos.",
                styles["Normal"],
            )
        )
        story.append(Spacer(1, 12))

    scores = battery.scores or {}

    if scores.get("etamiofe_score") is not None:
        story.append(Paragraph("Escore total AMIOFE-E (ETAMIOFE)", styles["Heading2"]))
        story.append(
            Paragraph(
                f"ETAMIOFE: {scores.get('etamiofe_score')}/{scores.get('etamiofe_max', 103)} — "
                f"{scores.get('severity_label', '—')}",
                styles["Normal"],
            )
        )
        dmo = "Presente" if scores.get("dmo_present") else "Ausente"
        story.append(Paragraph(f"DMO: {dmo} (corte ≥ {scores.get('dmo_cutoff', 89)})", styles["Normal"]))
        for cat_id, cat in (scores.get("categories") or {}).items():
            if isinstance(cat, dict):
                story.append(
                    Paragraph(
                        f"• {cat.get('title', cat_id)}: {cat.get('points', 0)}/{cat.get('max_sum', 0)}",
                        styles["Normal"],
                    )
                )
        story.append(Spacer(1, 12))

    if package.scoring.get("engine") in ("developmental_screening", "adl2"):
        story.append(Paragraph("Condições da aplicação", styles["Heading2"]))
        setup = scores.get("setup") or {}
        if setup.get("assessment_date"):
            story.append(Paragraph(f"Data da avaliação: {setup['assessment_date']}", styles["Normal"]))
        if setup.get("examiner_name"):
            story.append(Paragraph(f"Examinador: {setup['examiner_name']}", styles["Normal"]))
        if setup.get("initial_notes"):
            story.append(Paragraph(f"Observações iniciais: {setup['initial_notes']}", styles["Normal"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Resultados por domínio", styles["Heading2"]))
        for slug, domain_score in (scores.get("domains") or {}).items():
            if not isinstance(domain_score, dict):
                continue
            title = domain_score.get("title", slug)
            level = (
                domain_score.get("level_label")
                or domain_score.get("levelLabel")
                or domain_score.get("level", "—")
            )
            delays = domain_score.get("delay_count") or domain_score.get("delays") or 0
            line = f"• {title}: {level} — {delays} atraso(s)"
            if domain_score.get("standard_score") is not None:
                line += f" — EP {domain_score['standard_score']}"
            if domain_score.get("percentile") is not None:
                line += f" (P{domain_score['percentile']})"
            story.append(Paragraph(line, styles["Normal"]))
        story.append(Spacer(1, 12))

    story.append(Paragraph("Resultados por módulo", styles["Heading2"]))

    for slug, domain_score in (scores.get("domains") or {}).items():
        if isinstance(domain_score, dict):
            story.append(Paragraph(domain_score.get("summary", slug), styles["Normal"]))
            if domain_score.get("module_kind") == "phonology":
                for proc in domain_score.get("processes", [])[:10]:
                    persistent = " — persistente" if proc.get("persistent") else ""
                    story.append(
                        Paragraph(
                            f"• {proc.get('label')}: {proc.get('count')} ocorrências{persistent}",
                            styles["Normal"],
                        )
                    )
            if domain_score.get("module_kind") == "vocabulary":
                for cat in domain_score.get("categories", []):
                    story.append(
                        Paragraph(
                            f"• {cat.get('title')}: {cat.get('percentage')}% DVU",
                            styles["Normal"],
                        )
                    )
            if domain_score.get("module_kind") == "observational":
                level_label = (
                    domain_score.get("level_label")
                    or domain_score.get("levelLabel")
                    or domain_score.get("label")
                    or domain_score.get("level", "—")
                )
                story.append(
                    Paragraph(
                        f"• Classificação: {level_label} — {domain_score.get('percentage', 0)}%",
                        styles["Normal"],
                    )
                )
            story.append(Spacer(1, 8))

    clinical_conclusion = scores.get("clinical_conclusion") or scores.get("clinicalConclusion")
    if clinical_conclusion:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Conclusão clínica", styles["Heading2"]))
        story.append(Paragraph(str(clinical_conclusion), styles["Normal"]))

    story.append(Spacer(1, 12))
    story.append(Paragraph("Resumo", styles["Heading2"]))
    story.append(
        Paragraph(
            scores.get("interpretation") or scores.get("summary") or "Avaliação concluída.",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 24))
    story.append(Paragraph("Assinatura do profissional: _________________________", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()
