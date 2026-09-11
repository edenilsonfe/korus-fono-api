"""F6 — resumo do paciente em PDF (design B do spike 017).

Documento mínimo de continuidade do cuidado: identificação/diagnósticos, metas
atuais e as últimas N sessões (tipo/data). Nunca inclui anamnese, corpo de
evoluções, anexos ou outro conteúdo clínico — usa o mesmo recipe ReportLab e a
mesma identidade (``DocumentIdentity`` F2) dos relatórios F1.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from html import escape as _html_escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

from app.core.diagnosis_catalog import diagnosis_labels
from app.core.utils import calculate_age
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.session import Session
from app.services.report_export import DocumentIdentity

DOCUMENT_LABEL = "Resumo do paciente"
PURPOSE_NOTE = (
    "Documento de continuidade do cuidado. Não substitui o prontuário completo "
    "e não inclui anamnese, evoluções ou anexos."
)
EMPTY_GOALS_LABEL = "Nenhuma meta registrada."
EMPTY_SESSIONS_LABEL = "Nenhuma sessão registrada."
BULLET = "\u2022 "


def escape_paragraph_text(value: object) -> str:
    """Escape patient/author text for a ReportLab ``Paragraph`` (XML-like markup)."""
    return _html_escape(str(value if value is not None else ""), quote=False)


def _identity_headline(identity: DocumentIdentity | None) -> str:
    if identity is None:
        return ""
    name = (identity.professional_name or "").strip()
    council = (identity.council or "").strip()
    if name and council:
        return f"{name} — {council}"
    return name or council


def _image_flowable(data: bytes | None, *, width: float) -> Image | None:
    if not data:
        return None
    try:
        reader = ImageReader(io.BytesIO(data))
        original_width, original_height = reader.getSize()
        if not original_width or not original_height:
            return None
        return Image(io.BytesIO(data), width=width, height=width * original_height / original_width)
    except Exception:  # noqa: BLE001 - a broken image must not break the document
        return None


def _session_when_label(session: Session) -> str:
    if session.date is None:
        return ""
    return session.date.strftime("%d/%m/%Y %H:%M")


def _session_type_label(session: Session) -> str:
    return (session.type or "").strip() or "Sessão"


def _goal_line(goal: Goal) -> str:
    parts = [escape_paragraph_text((goal.title or "").strip())]
    area = (goal.area or "").strip()
    if area:
        parts.append(escape_paragraph_text(area))
    headline = " — ".join(part for part in parts if part)
    status = escape_paragraph_text((goal.status or "").strip())
    return f"{headline} — {goal.progress}% — {status}"


def export_patient_summary_pdf(
    *,
    patient: Patient,
    goals: list[Goal],
    sessions: list[Session],
    specialty_key: str = "fono",
    identity: DocumentIdentity | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """Render the minimized patient summary (design B) and return PDF bytes.

    ``sessions`` arrives already filtered/ordered by the caller (most recent
    first); the document never adds clinical text on its own.
    """
    when = generated_at or datetime.now(UTC)
    styles = getSampleStyleSheet()
    story: list = []

    logo = _image_flowable(identity.logo_bytes if identity else None, width=110)
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 8))
    story.extend(
        [
            Paragraph("KorusFono", styles["Title"]),
            Paragraph(DOCUMENT_LABEL, styles["Heading2"]),
            Paragraph(f"Paciente: {escape_paragraph_text(patient.name)}", styles["Normal"]),
            Paragraph(f"Data: {when.date().isoformat()}", styles["Normal"]),
        ]
    )
    headline = _identity_headline(identity)
    if headline:
        story.append(
            Paragraph(f"Profissional: {escape_paragraph_text(headline)}", styles["Normal"])
        )
    story.append(Paragraph(PURPOSE_NOTE, styles["Italic"]))
    story.append(Spacer(1, 12))

    _append_identification(story, styles, patient, specialty_key, when.date())
    _append_goals(story, styles, goals)
    _append_sessions(story, styles, sessions)

    if headline:
        story.append(Spacer(1, 24))
        signature = _image_flowable(
            identity.signature_bytes if identity else None, width=140
        )
        if signature is not None:
            story.append(signature)
            story.append(Spacer(1, 4))
        story.append(Paragraph(escape_paragraph_text(headline), styles["Normal"]))
        story.append(
            Paragraph(f"Emitido em {when.date().isoformat()}", styles["Normal"])
        )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    doc.build(story)
    return buffer.getvalue()


def _append_identification(story, styles, patient: Patient, specialty_key: str, today: date) -> None:
    story.append(Paragraph("Identificação", styles["Heading2"]))
    keys = patient.diagnosis_keys or []
    labels = diagnosis_labels(keys, specialty_key)
    diagnoses = "; ".join(escape_paragraph_text(label) for label in labels) or "Não informados"
    age = calculate_age(patient.birth_date, today)
    story.append(
        Paragraph(
            f"{BULLET}Nome: {escape_paragraph_text(patient.name)}",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"{BULLET}Nascimento: {patient.birth_date.strftime('%d/%m/%Y')} ({age} anos)",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"{BULLET}Status: {escape_paragraph_text(patient.status)} "
            f"· Início do acompanhamento: {patient.start_date.strftime('%d/%m/%Y')}",
            styles["Normal"],
        )
    )
    story.append(Paragraph(f"{BULLET}Diagnósticos: {diagnoses}", styles["Normal"]))
    story.append(Spacer(1, 6))


def _append_goals(story, styles, goals: list[Goal]) -> None:
    story.append(Paragraph("Metas (estado atual)", styles["Heading2"]))
    if not goals:
        story.append(Paragraph(EMPTY_GOALS_LABEL, styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for goal in goals:
        story.append(Paragraph(f"{BULLET}{_goal_line(goal)}", styles["Normal"]))
    story.append(Spacer(1, 6))


def _append_sessions(story, styles, sessions: list[Session]) -> None:
    story.append(
        Paragraph(f"Últimas {len(sessions)} sessões (mais recentes primeiro)", styles["Heading2"])
    )
    if not sessions:
        story.append(Paragraph(EMPTY_SESSIONS_LABEL, styles["Normal"]))
        return
    for session in sessions:
        story.append(
            Paragraph(
                f"{BULLET}{_session_when_label(session)} — "
                f"{escape_paragraph_text(_session_type_label(session))}",
                styles["Normal"],
            )
        )
