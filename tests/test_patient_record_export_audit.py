"""F6 — auditoria de exportação do prontuário (requested/generated/failed).

A tentativa autorizada é persistida como ``requested`` ANTES da geração, e o
resultado (``generated``/``failed``) numa segunda transação independente. Uma
falha nunca vira sucesso: o registro preserva o erro e o processo que cair no
meio deixa o ``requested`` visível. O log não duplica conteúdo clínico.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.anamnese import AnamneseEntry
from app.models.goal import Goal
from app.models.patient_record_export import PatientRecordExport
from app.models.session import Session
from app.schemas.patient_export import (
    PatientRecordExportRequest,
    PatientRecordExportResponse,
    PatientSummaryExportQuery,
)

ANAMNESE_MARKER = "ANAMNESE-AUDIT-7a1"


@pytest.fixture
def audit_session_factory(db_session, monkeypatch):
    factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(
        "app.services.patient_record_export.AsyncSessionLocal", factory
    )
    return factory


async def _seed_audit_fixture(db_session, patient):
    db_session.add(Goal(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        title="Meta auditada",
        area="Linguagem",
        progress=10,
        status="Inicial",
        start_date=datetime(2026, 1, 5).date(),
    ))
    db_session.add(Session(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        date=datetime(2026, 3, 15, 14, 0),
        duration=50,
        type="Terapia de linguagem",
        objectives=[],
        notes="",
    ))
    db_session.add(
        AnamneseEntry(patient_id=patient.id, section="queixa", value=ANAMNESE_MARKER)
    )
    await db_session.commit()


async def _audit_rows(db_session, patient_id):
    result = await db_session.execute(
        select(PatientRecordExport)
        .where(PatientRecordExport.patient_id == patient_id)
        .order_by(PatientRecordExport.requested_at.asc())
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_summary_export_records_requested_then_generated(
    api_client, auth_headers, db_session, patient, professional, audit_session_factory
):
    await _seed_audit_fixture(db_session, patient)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "generated"
    assert row.kind == "summary"
    assert row.format == "pdf"
    assert row.sections == ["identification", "goals", "sessions"]
    assert row.purpose == "care_continuity"
    assert row.professional_id == professional.id
    assert row.attachment_count == 0
    assert row.requested_at is not None
    assert row.completed_at is not None
    assert row.error_code is None
    assert row.record_counts == {"goals": 1, "sessions": 1}
    # O documento servido é exatamente o que foi auditado.
    assert row.size_bytes == len(response.content)
    import hashlib

    assert row.sha256 == hashlib.sha256(response.content).hexdigest()


@pytest.mark.asyncio
async def test_summary_export_persists_requested_before_generation(
    api_client, auth_headers, db_session, patient, audit_session_factory, monkeypatch
):
    """Prova que o registro ``requested`` já está commitado (visível de outra
    sessão) quando a geração termina — a queda entre as duas transações deixa
    a tentativa autorizada registrada, nunca um sucesso inventado."""
    from app.services import patient_record_export as prd

    observed: dict = {}
    real_finalize = prd.finalize_patient_record_export

    async def spy_finalize(export_id, **kwargs):
        async with audit_session_factory() as session:
            row = await session.get(PatientRecordExport, export_id)
            observed["status"] = row.status
            observed["completed_at"] = row.completed_at
        await real_finalize(export_id, **kwargs)

    monkeypatch.setattr(prd, "finalize_patient_record_export", spy_finalize)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert observed["status"] == "requested"
    assert observed["completed_at"] is None
    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    assert rows[0].status == "generated"


@pytest.mark.asyncio
async def test_summary_export_failure_marks_audit_failed_and_returns_503(
    api_client, auth_headers, db_session, patient, audit_session_factory, monkeypatch
):
    from app.services import patient_record_export as prd

    def _boom(**_kwargs):
        raise RuntimeError("renderer indisponível")

    monkeypatch.setattr(prd, "export_patient_summary_pdf", _boom)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert "resumo" in response.json()["detail"]

    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failed"
    assert row.error_code == "render_failed"
    assert row.completed_at is not None
    assert row.requested_at is not None
    assert row.size_bytes is None
    assert row.sha256 is None


@pytest.mark.asyncio
async def test_audit_failure_does_not_serve_the_document(
    api_client, auth_headers, db_session, patient, audit_session_factory, monkeypatch
):
    """Sem conseguir finalizar a auditoria, o PDF não é devolvido: o registro
    fica ``requested`` (visível) em vez de fingir um sucesso."""
    from app.services import patient_record_export as prd

    async def _boom(export_id, **kwargs):
        raise RuntimeError("auditoria indisponível")

    monkeypatch.setattr(prd, "finalize_patient_record_export", _boom)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )

    assert response.status_code == 503
    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    assert rows[0].status == "requested"
    assert rows[0].completed_at is None


@pytest.mark.asyncio
async def test_each_summary_export_creates_its_own_audit_row(
    api_client, auth_headers, db_session, patient, audit_session_factory
):
    for _ in range(2):
        response = await api_client.get(
            f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
        )
        assert response.status_code == 200, response.text
    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 2
    assert all(row.status == "generated" for row in rows)
    assert rows[0].id != rows[1].id


@pytest.mark.asyncio
async def test_audit_row_has_no_duplicated_clinical_content(
    api_client, auth_headers, db_session, patient, audit_session_factory
):
    await _seed_audit_fixture(db_session, patient)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, patient.id)
    row = rows[0]
    persisted = str({column.name: getattr(row, column.name) for column in row.__table__.columns})
    for secret in (ANAMNESE_MARKER, "João Silva", "Meta auditada", "Terapia de linguagem"):
        assert secret not in persisted, f"auditoria duplicou conteúdo clínico: {secret}"


@pytest.mark.asyncio
async def test_begin_and_finalize_audit_are_independent_transactions(
    db_session, patient, professional, audit_session_factory
):
    from app.services import patient_record_export as prd

    export_id = await prd.begin_patient_record_export(
        patient_id=patient.id,
        professional_id=professional.id,
        kind="summary",
        export_format="pdf",
        sections=("identification", "goals", "sessions"),
        purpose="care_continuity",
        attachment_count=0,
    )

    async with audit_session_factory() as other_session:
        pending = await other_session.get(PatientRecordExport, export_id)
        assert pending.status == "requested"
        assert pending.completed_at is None
        assert pending.requested_at is not None

    await prd.finalize_patient_record_export(
        export_id,
        result_status="generated",
        record_counts={"goals": 0, "sessions": 0},
        size_bytes=123,
        sha256="a" * 64,
    )

    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    assert rows[0].status == "generated"
    assert rows[0].size_bytes == 123
    assert rows[0].sha256 == "a" * 64
    assert rows[0].completed_at is not None


# --- Contrato dos schemas do dossiê (implementação completa na tarefa 3.2) ---


def _request_payload(**overrides):
    payload = {
        "format": "pdf",
        "sections": ["identification", "goals"],
        "purpose": "care_continuity",
        "confirmSensitiveContent": True,
    }
    payload.update(overrides)
    return payload


def test_record_export_request_accepts_the_wire_contract():
    body = PatientRecordExportRequest(
        **_request_payload(
            **{"from": "2026-01-01", "to": "2026-03-31", "attachmentIds": []}
        )
    )
    assert body.from_date.isoformat() == "2026-01-01"
    assert body.to_date.isoformat() == "2026-03-31"
    assert body.attachment_ids == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknownField": True},
        {"sections": ["goals"]},
        {"sections": ["identification", "goals", "goals"]},
        {"from": "2026-03-31", "to": "2026-01-01"},
        {"sections": ["identification"], "attachmentIds": [str(uuid4())]},
        {"format": "docx"},
        {"purpose": "curiosity"},
    ],
)
def test_record_export_request_rejects_invalid_selection(overrides):
    with pytest.raises(ValidationError):
        PatientRecordExportRequest(**_request_payload(**overrides))


def test_summary_query_default_and_bounds():
    assert PatientSummaryExportQuery().sessions_limit == 10
    assert PatientSummaryExportQuery(sessionsLimit=50).sessions_limit == 50
    for invalid in (0, 51, -1):
        with pytest.raises(ValidationError):
            PatientSummaryExportQuery(sessions_limit=invalid)


def test_record_export_response_serializes_camel_case_with_nullable_result():
    response = PatientRecordExportResponse(
        id=str(uuid4()),
        kind="summary",
        format="pdf",
        sections=["identification", "goals", "sessions"],
        purpose="care_continuity",
        status="generated",
        requested_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        completed_at=None,
        actor_professional_id=str(uuid4()),
        record_counts={"goals": 2, "sessions": 10},
        attachment_count=0,
        size_bytes=2048,
        sha256="b" * 64,
        error_code=None,
    )
    data = response.model_dump(by_alias=True, mode="json")
    assert set(data) == {
        "id",
        "kind",
        "format",
        "sections",
        "from",
        "to",
        "purpose",
        "status",
        "requestedAt",
        "completedAt",
        "actorProfessionalId",
        "recordCounts",
        "attachmentCount",
        "sizeBytes",
        "sha256",
        "errorCode",
    }
    assert data["requestedAt"] == "2026-09-11T12:00:00Z"
    assert data["completedAt"] is None
    assert data["attachmentCount"] == 0
    assert data["errorCode"] is None
