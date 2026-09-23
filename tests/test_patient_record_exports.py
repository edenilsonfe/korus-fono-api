"""Cobertura focada das seções clínicas novas do dossiê."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from app.models.clinical_review import ClinicalReview
from app.models.intake import IntakeFile, IntakeRequest
from app.schemas.patient_export import PatientRecordExportRequest
from app.services import patient_record_export as export_service
from app.services.patient_record_export import (
    collect_patient_record_export,
    render_patient_record_pdf,
)
from app.services.clinical_review_service import _snapshot


@pytest.mark.asyncio
async def test_collect_export_includes_completed_review_and_submitted_intake_only(
    db_session, patient, monkeypatch
):
    completed_at = datetime(2026, 3, 20, 15, 0, tzinfo=UTC)
    source_id = "00000000-0000-0000-0000-000000000001"
    comparison_target_id = "00000000-0000-0000-0000-000000000002"
    review = ClinicalReview(
        patient_id=patient.id,
        author_professional_id=patient.professional_id,
        kind="discharge",
        status="completed",
        summary="Resumo da revisão",
        discharge_on=date(2026, 3, 18),
        discharge_reason="Objetivos do ciclo concluídos",
        return_recommended=True,
        return_on=date(2026, 4, 20),
        goal_decisions=[{"goalId": source_id, "decision": "close", "note": "Decisão registrada"}],
        appointment_decisions=[{"appointmentId": comparison_target_id, "action": "keep"}],
        source_snapshot=[
            _snapshot(
                kind="comparison",
                source_id=UUID(source_id),
                comparison_target_id=UUID(comparison_target_id),
                version=1,
                fingerprint="source-fingerprint",
                source_date=datetime(2026, 3, 19, tzinfo=UTC).date(),
                author_name="Profissional autora",
                excerpt="Fonte clínica",
            )
        ],
        completed_at=completed_at,
        completed_by_professional_id=patient.professional_id,
    )
    submitted = IntakeRequest(
        patient_id=patient.id,
        owner_professional_id=patient.professional_id,
        caregiver_name_snapshot="Responsável",
        status="submitted",
        responses={"reasonForReferral": {"value": "Motivo registrado", "notKnown": False}},
        submitted_at=completed_at,
    )
    draft = IntakeRequest(
        patient_id=patient.id,
        owner_professional_id=patient.professional_id,
        caregiver_name_snapshot="Responsável",
        status="draft",
        responses={"reasonForReferral": {"value": "Rascunho secreto", "notKnown": False}},
    )
    db_session.add_all([review, submitted, draft])
    await db_session.flush()
    source_file = IntakeFile(
        intake_request_id=submitted.id,
        name="laudo-origem.pdf",
        content_type="application/pdf",
        size_bytes=128,
        storage_key="intake/secret/storage-key",
    )
    db_session.add(source_file)
    await db_session.commit()

    request = PatientRecordExportRequest(
        format="pdf",
        sections=["identification", "clinical_reviews", "intake"],
        purpose="professional_archive",
        confirm_sensitive_content=True,
    )
    records = await collect_patient_record_export(
        db_session, patient=patient, request=request
    )

    assert records.sections == ["identification", "clinical_reviews", "intake"]
    assert records.clinical_reviews == [review]
    assert records.intakes == [submitted]
    assert records.intake_files[submitted.id] == [source_file]
    assert records.clinical_reviews[0].source_snapshot[0]["sourceDate"] == "2026-03-19"
    assert records.clinical_reviews[0].source_snapshot[0]["comparisonTargetId"] == comparison_target_id
    assert records.counts == {"clinical_reviews": 1, "intake": 1}
    assert records.dated_record_count == 2
    assert all(item.id != draft.id for item in records.intakes)

    class CapturedParagraph:
        def __init__(self, text, _style):
            self.text = text

    captured: list[CapturedParagraph] = []
    monkeypatch.setattr(export_service, "Paragraph", CapturedParagraph)
    monkeypatch.setattr(export_service, "Spacer", lambda *_args: None)
    styles = {"Heading2": None, "Normal": None, "Italic": None}
    export_service._append_clinical_reviews(captured, styles, records)
    export_service._append_intake(captured, styles, records)
    story_text = "\n".join(item.text for item in captured if item is not None)
    assert "2026-03-19" in story_text
    assert "Profissional autora" in story_text
    assert comparison_target_id in story_text
    assert "informante: Responsável" in story_text
    assert "Pré-atendimento — motivo da procura: Motivo registrado" in story_text
    assert "Data efetiva da alta: 18/03/2026" in story_text
    assert "Motivo da alta: Objetivos do ciclo concluídos" in story_text
    assert "Retorno previsto: 20/04/2026" in story_text
    assert "Retorno recomendado: Sim" in story_text
    assert f"Meta {source_id}: Encerrar — Decisão registrada" in story_text
    assert f"Consulta {comparison_target_id}: Manter" in story_text

    monkeypatch.undo()
    pdf = render_patient_record_pdf(
        patient=patient,
        records=records,
        export_id=review.id,
        purpose="professional_archive",
        generated_at=completed_at,
    )
    assert pdf.startswith(b"%PDF")
    assert b"intake/secret/storage-key" not in pdf
