"""F3 — report revision/versioning with optimistic control and delivery snapshots.

Behaviour under test:

- ``AIReport.version`` starts at 1; every change writes an ``AIReportRevision``
  carrying the previous version and bumps the report version; a no-op keeps it.
- Consolidated reports require ``expectedVersion`` + ``status`` on PATCH and keep
  the four fixed sections; a stale version is rejected (two editors).
- New deliveries freeze the delivered text (``ReportDelivery.document_snapshot``)
  with ``reportVersion``/``contentHash``; the old public link keeps its text after
  the report is revised. Legacy rows without a snapshot stay ``legacy_live``.
"""

import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from docx import Document
from sqlalchemy import select

from app.models.ai import AIReport
from app.models.assessment import Assessment
from app.models.report_composition import AIReportComposition
from app.models.report_delivery import ReportDelivery

LLM_DRAFT = (
    "## Síntese\nO paciente demonstra avanço nas habilidades consolidadas "
    "dos instrumentos selecionados.\n\n"
    "## Conduta\nManter terapia semanal e reavaliar em três meses."
)

CONSOLIDATED_SECTIONS = ("## Identificação", "## Instrumentos", "## Síntese", "## Conduta")

SNAPSHOT_KEYS = {
    "formatVersion",
    "reportType",
    "reportDate",
    "patientName",
    "professionalName",
    "professionalCouncil",
    "reportVersion",
    "contentHash",
    "content",
    "capturedAt",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value=LLM_DRAFT)
    monkeypatch.setattr("app.services.report_composition_service.run_llm", mock)
    return mock


async def _make_report(
    db_session, professional, patient, *, content, status="finalized", report_type="clinico"
):
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type=report_type,
        date=date(2026, 9, 1),
        preview=content[:200],
        content=content,
        status=status,
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


async def _patch(api_client, auth_headers, report_id, payload):
    return await api_client.patch(
        f"/api/v1/ai/reports/{report_id}", headers=auth_headers, json=payload
    )


async def _revisions(api_client, auth_headers, report_id):
    response = await api_client.get(
        f"/api/v1/ai/reports/{report_id}/revisions", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create_delivery(api_client, auth_headers, report, body=None):
    return await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json=body or {"channel": "link"},
    )


async def _add_completed_assessment(db_session, patient, professional, protocol_id, day):
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=professional.id,
        protocol_id=protocol_id,
        date=day,
        result="Atraso leve",
        percentage=50,
        interpretation="",
        fields=[],
        answers={"1": "sim"},
        scores={"domains": {"linguagem": 3}, "total": 3},
        status="completed",
        informant="Mãe",
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


async def _create_consolidated(api_client, auth_headers, db_session, professional, patient):
    """Two completed assessments of distinct protocols -> one draft laudo."""
    assessments = [
        await _add_completed_assessment(
            db_session, patient, professional, "portage", date(2026, 1, 10)
        ),
        await _add_completed_assessment(
            db_session, patient, professional, "vanderbilt", date(2026, 2, 10)
        ),
    ]
    response = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "consolidado",
            "composition": {"assessmentIds": [str(item.id) for item in assessments]},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Revision/versioning
# --------------------------------------------------------------------------- #


async def test_version_starts_at_one_and_revision_copies_previous_version(
    api_client, auth_headers, db_session, professional, patient
):
    original = "## Sessão\nTexto original do relatório."
    report = await _make_report(db_session, professional, patient, content=original)
    assert report.version == 1

    detail = await api_client.get(f"/api/v1/ai/reports/{report.id}", headers=auth_headers)
    assert detail.json()["version"] == 1

    revised = "## Sessão\nTexto revisado do relatório."
    response = await _patch(
        api_client, auth_headers, report.id, {"content": revised, "status": "finalized"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2

    history = await _revisions(api_client, auth_headers, report.id)
    assert len(history) == 1
    assert history[0]["content"] == original
    assert history[0]["status"] == "finalized"
    assert history[0]["version"] == 1

    # A no-op keeps the current version and does not create another revision.
    noop = await _patch(
        api_client, auth_headers, report.id, {"content": revised, "status": "finalized"}
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["version"] == 2
    assert len(await _revisions(api_client, auth_headers, report.id)) == 1


async def test_legacy_patch_remains_compatible_when_new_fields_are_omitted(
    api_client, auth_headers, db_session, professional, patient
):
    report = await _make_report(
        db_session, professional, patient, content="Rascunho", status="draft"
    )
    # Legacy payload: no expectedVersion, no status -> legacy default (finalize).
    response = await _patch(api_client, auth_headers, report.id, {"content": "Rascunho final"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "finalized"
    assert response.json()["version"] == 2


async def test_stale_expected_version_is_rejected_for_common_report(
    api_client, auth_headers, db_session, professional, patient
):
    report = await _make_report(db_session, professional, patient, content="## Sessão\nBase.")

    first = await _patch(
        api_client,
        auth_headers,
        report.id,
        {"content": "## Sessão\nEditor A.", "status": "finalized", "expectedVersion": 1},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    # Editor B still holds version 1.
    stale = await _patch(
        api_client,
        auth_headers,
        report.id,
        {"content": "## Sessão\nEditor B.", "status": "finalized", "expectedVersion": 1},
    )
    assert stale.status_code == 409
    assert "alterado" in stale.json()["detail"]

    current = await api_client.get(f"/api/v1/ai/reports/{report.id}", headers=auth_headers)
    assert current.json()["content"] == "## Sessão\nEditor A."
    assert current.json()["version"] == 2
    assert len(await _revisions(api_client, auth_headers, report.id)) == 1


# --------------------------------------------------------------------------- #
# Consolidated reports: mandatory optimistic control and fixed sections
# --------------------------------------------------------------------------- #


async def test_consolidated_patch_requires_expected_version_and_status(
    api_client, auth_headers, db_session, professional, patient, llm
):
    data = await _create_consolidated(api_client, auth_headers, db_session, professional, patient)
    report_id = data["id"]
    assert data["version"] == 1

    missing_both = await _patch(api_client, auth_headers, report_id, {"content": data["content"]})
    assert missing_both.status_code == 422
    assert "expectedVersion" in missing_both.json()["detail"]

    missing_status = await _patch(
        api_client, auth_headers, report_id, {"content": data["content"], "expectedVersion": 1}
    )
    assert missing_status.status_code == 422
    assert "status" in missing_status.json()["detail"].lower()

    stale = await _patch(
        api_client,
        auth_headers,
        report_id,
        {"content": data["content"], "expectedVersion": 2, "status": "draft"},
    )
    assert stale.status_code == 409


async def test_consolidated_revision_bumps_version_and_keeps_composition(
    api_client, auth_headers, db_session, professional, patient, llm
):
    data = await _create_consolidated(api_client, auth_headers, db_session, professional, patient)
    report_id = data["id"]

    composition_before = await api_client.get(
        f"/api/v1/ai/reports/{report_id}/composition", headers=auth_headers
    )
    assert composition_before.status_code == 200, composition_before.text
    before = composition_before.json()

    revised_content = data["content"] + "\n\nRevisão clínica aprovada pela profissional."
    saved = await _patch(
        api_client,
        auth_headers,
        report_id,
        {"content": revised_content, "status": "finalized", "expectedVersion": 1},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert saved.json()["status"] == "finalized"

    history = await _revisions(api_client, auth_headers, report_id)
    assert len(history) == 1
    assert history[0]["version"] == 1
    assert history[0]["content"] == data["content"]

    # The composition snapshot captured at generation time never changes.
    composition_after = await api_client.get(
        f"/api/v1/ai/reports/{report_id}/composition", headers=auth_headers
    )
    after = composition_after.json()
    assert after["contextHash"] == before["contextHash"]
    assert after["sources"] == before["sources"]
    assert after["capturedAt"] == before["capturedAt"]
    stored = await db_session.scalar(
        select(AIReportComposition).where(AIReportComposition.report_id == UUID(report_id))
    )
    assert stored is not None
    assert stored.context_hash == before["contextHash"]

    # The four fixed sections are part of the frozen template.
    sectionless = revised_content.replace("## Conduta", "Conduta final")
    missing_section = await _patch(
        api_client,
        auth_headers,
        report_id,
        {"content": sectionless, "status": "finalized", "expectedVersion": 2},
    )
    assert missing_section.status_code == 422
    assert "Conduta" in missing_section.json()["detail"]

    out_of_order = "## Conduta\nx\n\n## Síntese\ny\n\n## Identificação\nz\n\n## Instrumentos\nw"
    unordered = await _patch(
        api_client,
        auth_headers,
        report_id,
        {"content": out_of_order, "status": "finalized", "expectedVersion": 2},
    )
    assert unordered.status_code == 422

    # Finalized -> draft stays blocked.
    back_to_draft = await _patch(
        api_client,
        auth_headers,
        report_id,
        {"content": revised_content, "status": "draft", "expectedVersion": 2},
    )
    assert back_to_draft.status_code == 409

    # Two editors holding version 2: only one wins.
    winner = await _patch(
        api_client,
        auth_headers,
        report_id,
        {
            "content": revised_content + "\nEditor 1",
            "status": "finalized",
            "expectedVersion": 2,
        },
    )
    assert winner.status_code == 200, winner.text
    assert winner.json()["version"] == 3
    loser = await _patch(
        api_client,
        auth_headers,
        report_id,
        {
            "content": revised_content + "\nEditor 2",
            "status": "finalized",
            "expectedVersion": 2,
        },
    )
    assert loser.status_code == 409


async def test_consolidated_noop_keeps_version(
    api_client, auth_headers, db_session, professional, patient, llm
):
    data = await _create_consolidated(api_client, auth_headers, db_session, professional, patient)
    response = await _patch(
        api_client,
        auth_headers,
        data["id"],
        {"content": data["content"], "status": "draft", "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert await _revisions(api_client, auth_headers, data["id"]) == []


# --------------------------------------------------------------------------- #
# Delivery snapshots
# --------------------------------------------------------------------------- #


async def test_draft_reports_cannot_be_delivered(
    api_client, auth_headers, db_session, professional, patient, llm
):
    draft = await _make_report(
        db_session, professional, patient, content="Rascunho", status="draft"
    )
    blocked = await _create_delivery(api_client, auth_headers, draft)
    assert blocked.status_code == 409

    consolidated = await _create_consolidated(
        api_client, auth_headers, db_session, professional, patient
    )
    blocked_consolidated = await api_client.post(
        f"/api/v1/ai/reports/{consolidated['id']}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert blocked_consolidated.status_code == 409


async def test_delivery_snapshot_freezes_text_and_version_after_edit(
    api_client, auth_headers, db_session, professional, patient
):
    original = "## Sessão\nTexto entregue à família."
    report = await _make_report(db_session, professional, patient, content=original)

    created = await _create_delivery(api_client, auth_headers, report)
    assert created.status_code == 201, created.text
    delivery = created.json()
    assert delivery["reportVersion"] == 1
    assert delivery["contentHash"] == _sha256(original)
    assert delivery["snapshotMode"] == "fixed"
    token = delivery["url"].rsplit("/", 1)[-1]

    edited = await _patch(
        api_client,
        auth_headers,
        report.id,
        {"content": "## Sessão\nTexto corrigido depois.", "status": "finalized"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["content"] == original
    assert body["reportVersion"] == 1
    assert body["contentHash"] == _sha256(original)
    assert body["snapshotMode"] == "fixed"

    exported = await api_client.get(
        f"/api/v1/report-deliveries/{token}/export", params={"format": "txt"}
    )
    assert exported.status_code == 200
    text = exported.content.decode("utf-8")
    assert "Texto entregue à família." in text
    assert "Texto corrigido depois." not in text

    listed = await api_client.get(
        f"/api/v1/ai/reports/{report.id}/deliveries", headers=auth_headers
    )
    row = next(item for item in listed.json() if item["id"] == delivery["id"])
    assert row["reportVersion"] == 1
    assert row["contentHash"] == _sha256(original)
    assert row["snapshotMode"] == "fixed"

    # A new delivery fixes the new version; the old link keeps the old text.
    second = await _create_delivery(api_client, auth_headers, report)
    assert second.status_code == 201, second.text
    assert second.json()["reportVersion"] == 2
    assert second.json()["contentHash"] == _sha256("## Sessão\nTexto corrigido depois.")
    second_token = second.json()["url"].rsplit("/", 1)[-1]
    assert (
        await api_client.get(f"/api/v1/report-deliveries/{second_token}")
    ).json()["content"] == "## Sessão\nTexto corrigido depois."
    assert (await api_client.get(f"/api/v1/report-deliveries/{token}")).json()["content"] == original


async def test_legacy_delivery_without_snapshot_reads_live_report(
    api_client, auth_headers, db_session, professional, patient
):
    report = await _make_report(
        db_session, professional, patient, content="## Sessão\nTexto antigo entregue."
    )
    token = "legacy-delivery-token-123"
    legacy = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=_sha256(token),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot=None,
    )
    db_session.add(legacy)
    await db_session.commit()

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["content"] == "## Sessão\nTexto antigo entregue."
    assert body["snapshotMode"] == "legacy_live"
    assert body["reportVersion"] is None
    assert body["contentHash"] is None

    # Legacy rows keep reading the current report; no invented historical text.
    await _patch(
        api_client,
        auth_headers,
        report.id,
        {"content": "## Sessão\nTexto atualizado no relatório."},
    )
    updated = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert updated.json()["content"] == "## Sessão\nTexto atualizado no relatório."
    assert updated.json()["snapshotMode"] == "legacy_live"

    exported = await api_client.get(
        f"/api/v1/report-deliveries/{token}/export", params={"format": "txt"}
    )
    assert exported.status_code == 200
    assert "Texto atualizado no relatório." in exported.content.decode("utf-8")


async def test_public_export_renders_all_formats_from_fixed_snapshot(
    api_client, auth_headers, db_session, professional, patient
):
    original = "## Sessão\nConteúdo congelado com identidade textual."
    report = await _make_report(db_session, professional, patient, content=original)
    created = await _create_delivery(api_client, auth_headers, report)
    assert created.status_code == 201, created.text
    token = created.json()["url"].rsplit("/", 1)[-1]

    # Editing the report and the professional profile afterwards must not change
    # the delivered text nor its textual identity.
    await _patch(
        api_client,
        auth_headers,
        report.id,
        {"content": "## Sessão\nConteúdo novo do relatório."},
    )
    professional.name = "Dra. Alterada"
    await db_session.commit()

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    assert public.json()["professionalName"] == "Dra. Teste"
    assert public.json()["content"] == original

    media_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "md": "text/markdown",
    }
    for fmt, media_type in media_types.items():
        export = await api_client.get(
            f"/api/v1/report-deliveries/{token}/export", params={"format": fmt}
        )
        assert export.status_code == 200, (fmt, export.text)
        assert export.headers["content-type"].startswith(media_type)

    txt = (
        await api_client.get(
            f"/api/v1/report-deliveries/{token}/export", params={"format": "txt"}
        )
    ).content.decode("utf-8")
    assert "Conteúdo congelado com identidade textual." in txt
    assert "Conteúdo novo do relatório." not in txt
    assert "Dra. Teste" in txt
    assert "Dra. Alterada" not in txt

    md = (
        await api_client.get(
            f"/api/v1/report-deliveries/{token}/export", params={"format": "md"}
        )
    ).content.decode("utf-8")
    assert "Conteúdo congelado com identidade textual." in md
    assert "Dra. Alterada" not in md

    pdf = (
        await api_client.get(
            f"/api/v1/report-deliveries/{token}/export", params={"format": "pdf"}
        )
    ).content
    assert pdf.startswith(b"%PDF")

    docx_bytes = (
        await api_client.get(
            f"/api/v1/report-deliveries/{token}/export", params={"format": "docx"}
        )
    ).content
    doc = Document(io.BytesIO(docx_bytes))
    paragraphs = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Conteúdo congelado com identidade textual." in paragraphs
    assert "Conteúdo novo do relatório." not in paragraphs
    assert "Dra. Teste" in paragraphs
    assert "CREFITO" in paragraphs


async def test_delivery_snapshot_stores_only_text_and_metadata(
    api_client, auth_headers, db_session, professional, patient
):
    original = "## Sessão\nTexto clínico congelado."
    report = await _make_report(db_session, professional, patient, content=original)
    created = await _create_delivery(api_client, auth_headers, report)
    assert created.status_code == 201, created.text
    token = created.json()["url"].rsplit("/", 1)[-1]

    delivery = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(created.json()["id"]))
    )
    await db_session.refresh(delivery)
    snapshot = delivery.document_snapshot
    assert snapshot is not None
    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot["content"] == original
    assert snapshot["contentHash"] == _sha256(original)
    assert snapshot["reportVersion"] == 1
    assert snapshot["reportType"] == "clinico"
    assert snapshot["patientName"] == "João Silva"
    assert snapshot["professionalName"] == "Dra. Teste"

    serialized = json.dumps(snapshot)
    assert token not in serialized
    assert "token" not in serialized.lower()
    assert "answers" not in serialized
    assert "/relatorio/" not in serialized


async def test_consolidated_delivery_snapshot_freezes_sections(
    api_client, auth_headers, db_session, professional, patient, llm
):
    data = await _create_consolidated(api_client, auth_headers, db_session, professional, patient)
    finalized = await _patch(
        api_client,
        auth_headers,
        data["id"],
        {"content": data["content"], "status": "finalized", "expectedVersion": 1},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "finalized"

    created = await api_client.post(
        f"/api/v1/ai/reports/{data['id']}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert created.status_code == 201, created.text
    token = created.json()["url"].rsplit("/", 1)[-1]
    assert created.json()["reportVersion"] == 2
    assert created.json()["snapshotMode"] == "fixed"

    revised = data["content"] + "\n\n## Observação final\nTexto novo após entrega."
    second = await _patch(
        api_client,
        auth_headers,
        data["id"],
        {"content": revised, "status": "finalized", "expectedVersion": 2},
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 3

    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["reportVersion"] == 2
    assert body["snapshotMode"] == "fixed"
    assert body["content"] == data["content"]
    assert "Observação final" not in body["content"]
    for heading in CONSOLIDATED_SECTIONS:
        assert heading in body["content"]
    assert body["reportTypeLabel"] == "Laudo Consolidado"
