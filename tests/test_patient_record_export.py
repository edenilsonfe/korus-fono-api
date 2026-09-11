"""F6 — POST /patients/{id}/record-exports (dossiê PDF) e GET do histórico.

O dossiê é selecionável (identificação obrigatória + seções opcionais), com
período em datas locais da clínica, limites duros (rejeitar, nunca truncar),
marca d'água em TODAS as páginas e auditoria ``requested`` → ``generated`` /
``failed``. O renderer real é exercitado; somente a identidade (evita S3) e o
storage de anexos são substituídos. PDF validado com pypdf.
"""

import hashlib
import io
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models.anamnese import AnamneseEntry
from app.models.assessment import Assessment
from app.models.attachment import Attachment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.patient_record_export import PatientRecordExport
from app.models.professional import Professional
from app.models.session import Session
from app.services import patient_record_export as prd
from app.services.storage import storage_service

ANAMNESE_MARKER = "ANAMNESE-DOSSIE-1a1"
ASSESSMENT_MARKER = "RESULTADO-ABFW-1a2"
ASSESSMENT_INTERPRETATION = "Interpretação clínica persistida 1a3"
ASSESSMENT_RAW_MARKER = "RAW-ANSWERS-NAO-EXPORTAR-1a4"
NORM_MARKER = "PERCENTIL-NORMA-NAO-EXPORTAR-1a5"
EVOLUTION_MARKER = "EVOLUCAO-DOSSIE-1a6"
EVOLUTION_TITLE = "Título da evolução 1a7"
GOAL_MARKER = "META-DOSSIE-1a8"
SESSION_MARKER = "TipoSessaoDossie1a9"
ATTACHMENT_BODY_MARKER = b"CONTEUDO-BINARIO-DO-ANEXO-1b1"
OTHER_PATIENT_MARKER = "PACIENTE-ALHEIO-NAO-EXPORTAR-1b2"


def _payload(**overrides) -> dict:
    payload = {
        "format": "pdf",
        "sections": ["identification", "anamnesis", "goals"],
        "purpose": "care_continuity",
        "confirmSensitiveContent": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def export_session_factory(db_session, monkeypatch):
    factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr("app.services.patient_record_export.AsyncSessionLocal", factory)
    return factory


@pytest.fixture
def document_identity(monkeypatch):
    from app.services.report_export import DocumentIdentity

    identity = DocumentIdentity(
        professional_name="Dra. Teste", council="CREFITO-4a 12345"
    )

    async def fake_identity(professional):
        return identity

    monkeypatch.setattr(prd, "build_document_identity", fake_identity)
    return identity


@pytest.fixture
def attachment_storage(monkeypatch):
    """Storage de anexos mockado nas convenções do repo (sem S3/MinIO)."""
    bodies: dict[str, bytes] = {}

    async def fake_download_limited(key, max_bytes, timeout_seconds=30):
        if key not in bodies:
            raise RuntimeError(f"objeto ausente: {key}")
        body = bodies[key]
        if len(body) > max_bytes:
            from app.services.storage import StorageLimitExceededError

            raise StorageLimitExceededError("estouro")
        return body, "application/pdf"

    monkeypatch.setattr(storage_service, "download_limited", fake_download_limited)
    return bodies


def pdf_pages_text(data: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]


def pdf_all_text(data: bytes) -> str:
    return "\n".join(pdf_pages_text(data))


async def _audit_rows(db_session, patient_id):
    result = await db_session.execute(
        select(PatientRecordExport)
        .where(PatientRecordExport.patient_id == patient_id)
        .order_by(PatientRecordExport.requested_at.asc())
    )
    return list(result.scalars().all())


async def _add_anamnese(db_session, patient, *, section="queixa", value=ANAMNESE_MARKER):
    entry = AnamneseEntry(patient_id=patient.id, section=section, value=value)
    db_session.add(entry)
    await db_session.commit()
    return entry


async def _add_assessment(
    db_session,
    patient,
    *,
    protocol_id="abfw",
    day=date(2026, 3, 10),
    result=ASSESSMENT_MARKER,
    percentage=75,
    status="completed",
    interpretation=ASSESSMENT_INTERPRETATION,
    answers=None,
    scores=None,
):
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        protocol_id=protocol_id,
        date=day,
        result=result,
        percentage=percentage,
        interpretation=interpretation,
        fields=[],
        answers=answers or {"1": ASSESSMENT_RAW_MARKER},
        scores=scores or {"percentile": NORM_MARKER},
        status=status,
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


async def _add_evolution(
    db_session,
    patient,
    *,
    when,
    content=EVOLUTION_MARKER,
    title=EVOLUTION_TITLE,
    session_id=None,
    professional_id=None,
):
    evolution = Evolution(
        patient_id=patient.id,
        professional_id=professional_id or patient.professional_id,
        session_id=session_id,
        date=when,
        title=title,
        content=content,
    )
    db_session.add(evolution)
    await db_session.commit()
    await db_session.refresh(evolution)
    return evolution


async def _add_goal(db_session, patient, *, title=GOAL_MARKER, area="Linguagem", progress=40):
    goal = Goal(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        title=title,
        area=area,
        progress=progress,
        status="Em andamento",
        start_date=date(2026, 2, 1),
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _add_session(db_session, patient, *, when, type_=SESSION_MARKER, notes="", objectives=None):
    session = Session(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        date=when,
        duration=50,
        type=type_,
        objectives=objectives or [],
        notes=notes,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _add_attachment(
    db_session,
    patient,
    *,
    name="exame.pdf",
    category="relatorio",
    when=datetime(2026, 3, 5, 10, 0, tzinfo=UTC),
    body=ATTACHMENT_BODY_MARKER,
    storage_key=None,
):
    key = storage_key or f"patients/{patient.id}/attachments/{uuid.uuid4().hex}"
    attachment = Attachment(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        name=name,
        category=category,
        size_bytes=len(body),
        storage_key=key,
        date=when,
    )
    db_session.add(attachment)
    await db_session.commit()
    await db_session.refresh(attachment)
    return attachment


async def _post_export(api_client, auth_headers, patient, **overrides):
    return await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        json=_payload(**overrides),
    )


# --------------------------------------------------------------------------- #
# Contrato de limites (valores do §3.3 — rejeitar, nunca truncar)
# --------------------------------------------------------------------------- #


def test_dossier_limits_match_the_contract():
    assert prd.MAX_EXPORT_ATTACHMENTS == 20
    assert prd.MAX_EXPORT_ATTACHMENT_BYTES == 50 * 1024 * 1024
    assert prd.MAX_EXPORT_DATED_RECORDS == 500
    assert prd.MAX_EXPORT_TEXT_CHARS == 200_000
    assert prd.MAX_EXPORT_PAGES == 200
    # Operação síncrona (sem ARQ) com orçamento de API e concorrência limitada.
    assert prd.EXPORT_BUDGET_SECONDS <= 60
    assert prd.RENDER_TIMEOUT_SECONDS <= prd.EXPORT_BUDGET_SECONDS
    assert prd.RENDER_CONCURRENCY >= 1


# --------------------------------------------------------------------------- #
# PDF do dossiê
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dossier_pdf_renders_selected_sections_and_audits_generated(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    await _add_anamnese(db_session, patient)
    await _add_assessment(db_session, patient)
    session = await _add_session(
        db_session, patient, when=datetime(2026, 3, 15, 14, 0, tzinfo=UTC)
    )
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC),
        session_id=session.id,
    )
    # Sessão sem evolução: entra na seção de sessões (a ligada não se duplica).
    await _add_session(
        db_session, patient, when=datetime(2026, 3, 18, 9, 0, tzinfo=UTC)
    )
    await _add_goal(db_session, patient)
    attachment = await _add_attachment(db_session, patient)
    attachment_storage[attachment.storage_key] = ATTACHMENT_BODY_MARKER

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=[
            "identification",
            "anamnesis",
            "assessments",
            "evolutions",
            "goals",
            "sessions",
            "attachments",
        ],
        attachmentIds=[str(attachment.id)],
        purpose="professional_archive",
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    export_id = uuid.UUID(response.headers["x-export-id"])
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="prontuario-{export_id}.pdf"'
    )

    text = pdf_all_text(response.content)
    for marker in (
        "João Silva",
        "Transtorno do Espectro Autista",
        ANAMNESE_MARKER,
        ASSESSMENT_MARKER,
        ASSESSMENT_INTERPRETATION,
        EVOLUTION_MARKER,
        EVOLUTION_TITLE,
        GOAL_MARKER,
        SESSION_MARKER,
        attachment.name,
        "Arquivo profissional",
    ):
        assert marker in text, f"seção selecionada ausente no PDF: {marker}"
    assert "Dossiê do prontuário" in text
    # Índice de anexos não embute o binário original.
    assert ATTACHMENT_BODY_MARKER.decode() not in text
    # Nem answers/provas brutas nem resultado de norma.
    assert ASSESSMENT_RAW_MARKER not in text
    assert NORM_MARKER not in text

    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "generated"
    assert row.kind == "dossier"
    assert row.format == "pdf"
    assert row.purpose == "professional_archive"
    assert row.sections == [
        "identification",
        "anamnesis",
        "assessments",
        "evolutions",
        "goals",
        "sessions",
        "attachments",
    ]
    assert row.professional_id == professional.id
    assert row.attachment_count == 1
    assert row.record_counts == {
        "anamnesis": 1,
        "assessments": 1,
        "evolutions": 1,
        "goals": 1,
        "sessions": 1,
        "attachments": 1,
    }
    assert row.size_bytes == len(response.content)
    assert row.sha256 == hashlib.sha256(response.content).hexdigest()
    assert row.completed_at is not None
    assert row.error_code is None


@pytest.mark.asyncio
async def test_dossier_pdf_watermark_identifies_export_and_pagination_on_every_page(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    for index in range(40):
        await _add_evolution(
            db_session,
            patient,
            when=datetime(2026, 3, 1, 9, 0, tzinfo=UTC) + timedelta(days=index),
            content=f"Evolução {index} — " + ("texto clínico extenso " * 12),
            title=f"Título {index}",
        )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 200, response.text
    pages = pdf_pages_text(response.content)
    export_id = response.headers["x-export-id"]
    assert len(pages) > 1, "o teste exige documento multipágina"
    for index, page_text in enumerate(pages, start=1):
        assert "CONFIDENCIAL" in page_text, f"marca ausente na página {index}"
        assert export_id in page_text, f"identificador ausente na página {index}"
        assert f"Página {index}" in page_text, f"paginação ausente na página {index}"
    assert "DEMONSTRAÇÃO" not in "\n".join(pages)


@pytest.mark.asyncio
async def test_dossier_pdf_marks_demo_patient_as_demonstration(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    patient.is_demo = True
    await db_session.commit()

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 200, response.text
    pages = pdf_pages_text(response.content)
    assert pages
    for index, page_text in enumerate(pages, start=1):
        assert "DEMONSTRAÇÃO" in page_text, f"DEMONSTRAÇÃO ausente na página {index}"


@pytest.mark.asyncio
async def test_dossier_pdf_never_includes_unselected_sections(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    await _add_anamnese(db_session, patient)
    await _add_assessment(db_session, patient)
    await _add_evolution(
        db_session, patient, when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC)
    )
    await _add_goal(db_session, patient)
    await _add_session(db_session, patient, when=datetime(2026, 3, 15, 14, 0, tzinfo=UTC))
    attachment = await _add_attachment(db_session, patient)
    attachment_storage[attachment.storage_key] = ATTACHMENT_BODY_MARKER

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification"]
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    for marker in (
        ANAMNESE_MARKER,
        ASSESSMENT_MARKER,
        EVOLUTION_MARKER,
        GOAL_MARKER,
        SESSION_MARKER,
        attachment.name,
    ):
        assert marker not in text, f"seção não selecionada vazou: {marker}"
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].record_counts == {}
    assert rows[0].attachment_count == 0


@pytest.mark.asyncio
async def test_dossier_pdf_period_uses_clinic_local_dates_inclusively(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "clinic_timezone", "America/Sao_Paulo")

    # 31/03 22:00 local (01/04 01:00 UTC) — dentro; 01/04 00:30 local — fora.
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
        content="DENTRO-DO-PERIODO-1c1",
        title="",
    )
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 4, 1, 3, 30, tzinfo=UTC),
        content="FORA-DO-PERIODO-1c2",
        title="",
    )
    # 28/02 23:00 local (01/03 02:00 UTC) — antes do from local.
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 1, 2, 0, tzinfo=UTC),
        content="FORA-DO-PERIODO-1c3",
        title="",
    )
    # 01/03 00:30 local (01/03 03:30 UTC) — dentro.
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 1, 3, 30, tzinfo=UTC),
        content="DENTRO-DO-PERIODO-1c4",
        title="",
    )
    await _add_assessment(
        db_session, patient, day=date(2026, 3, 31), result="AVALIACAO-NO-LIMITE-1c5"
    )
    await _add_assessment(
        db_session, patient, day=date(2026, 4, 1), result="AVALIACAO-FORA-1c6"
    )

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "evolutions", "assessments"],
        **{"from": "2026-03-01", "to": "2026-03-31"},
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    assert "DENTRO-DO-PERIODO-1c1" in text
    assert "DENTRO-DO-PERIODO-1c4" in text
    assert "AVALIACAO-NO-LIMITE-1c5" in text
    assert "FORA-DO-PERIODO-1c2" not in text
    assert "FORA-DO-PERIODO-1c3" not in text
    assert "AVALIACAO-FORA-1c6" not in text

    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].from_date == date(2026, 3, 1)
    assert rows[0].to_date == date(2026, 3, 31)
    assert rows[0].record_counts == {"assessments": 1, "evolutions": 2}


@pytest.mark.asyncio
async def test_dossier_pdf_labels_draft_anamnesis_and_includes_completed_ones(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    await _add_anamnese(db_session, patient)

    draft = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "anamnesis"]
    )
    assert draft.status_code == 200, draft.text
    assert "Anamnese (rascunho" in pdf_all_text(draft.content)

    patient.anamnese_status = "completed"
    await db_session.commit()
    completed = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "anamnesis"]
    )
    assert completed.status_code == 200, completed.text
    text = pdf_all_text(completed.content)
    assert ANAMNESE_MARKER in text
    assert "Anamnese (rascunho" not in text


@pytest.mark.asyncio
async def test_dossier_pdf_assessments_require_completed_with_persisted_result(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    await _add_assessment(db_session, patient, result="COMPLETED-PERSISTIDO-1d1")
    await _add_assessment(
        db_session, patient, result="RASCUNHO-NAO-EXPORTAR-1d2", status="in_progress"
    )
    await _add_assessment(
        db_session, patient, result="", status="completed", interpretation=""
    )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "assessments"]
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    assert "COMPLETED-PERSISTIDO-1d1" in text
    assert "RASCUNHO-NAO-EXPORTAR-1d2" not in text
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].record_counts == {"assessments": 1}


@pytest.mark.asyncio
async def test_dossier_pdf_sessions_do_not_duplicate_evolution_by_session_id(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    session_with_evolution = await _add_session(
        db_session,
        patient,
        when=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        type_="SESSAO-COM-EVOLUCAO-1e1",
    )
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        content="EVOLUCAO-DA-SESSAO-1e2",
        session_id=session_with_evolution.id,
    )
    await _add_session(
        db_session,
        patient,
        when=datetime(2026, 3, 21, 11, 0, tzinfo=UTC),
        type_="SESSAO-SEM-EVOLUCAO-1e3",
    )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions", "sessions"]
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    assert "EVOLUCAO-DA-SESSAO-1e2" in text
    assert "SESSAO-COM-EVOLUCAO-1e1" not in text
    assert "SESSAO-SEM-EVOLUCAO-1e3" in text
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].record_counts == {"evolutions": 1, "sessions": 1}


@pytest.mark.asyncio
async def test_dossier_pdf_escapes_patient_author_and_clinical_text(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    patient.name = 'João & <b>Silva</b> "Aspas"'
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC),
        content="<script>alert(1)</script> & <?xml?>",
        title="Título <b>suspeito</b>",
    )
    await db_session.commit()

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    assert "João & <b>Silva</b>" in text
    assert "<script>alert(1)</script>" in text


@pytest.mark.asyncio
async def test_dossier_pdf_resolves_evolution_author_names(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    coauthor = Professional(
        email="coautora-dossie@test.com",
        password_hash="x",
        name="Dra. Coautora",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(coauthor)
    await db_session.flush()
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC),
        professional_id=coauthor.id,
    )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 200, response.text
    text = pdf_all_text(response.content)
    assert "Dra. Coautora" in text
    assert "Dra. Teste" in text


# --------------------------------------------------------------------------- #
# Limites — rejeitar nunca truncar
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dossier_rejects_more_than_twenty_attachments_with_413(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    ids = []
    for index in range(prd.MAX_EXPORT_ATTACHMENTS + 1):
        attachment = await _add_attachment(db_session, patient, name=f"anexo-{index}.pdf")
        ids.append(str(attachment.id))

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=ids,
    )

    assert response.status_code == 413
    assert "20 anexos" in response.json()["detail"]
    assert await _audit_rows(db_session, patient.id) == []


@pytest.mark.asyncio
async def test_dossier_rejects_declared_attachment_bytes_over_limit_with_413(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    monkeypatch.setattr(prd, "MAX_EXPORT_ATTACHMENT_BYTES", 100)
    first = await _add_attachment(db_session, patient, name="a.pdf", body=b"x" * 60)
    second = await _add_attachment(db_session, patient, name="b.pdf", body=b"y" * 60)

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(first.id), str(second.id)],
    )

    assert response.status_code == 413
    assert await _audit_rows(db_session, patient.id) == []


@pytest.mark.asyncio
async def test_dossier_rejects_more_than_max_dated_records(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    monkeypatch.setattr(prd, "MAX_EXPORT_DATED_RECORDS", 2)
    for index in range(3):
        await _add_session(
            db_session,
            patient,
            when=datetime(2026, 3, 10 + index, 9, 0, tzinfo=UTC),
            type_=f"SESSAO-LIMITE-{index}",
        )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "sessions"]
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert "registros datados" in response.json()["detail"]
    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "record_limit_exceeded"
    assert rows[0].size_bytes is None


@pytest.mark.asyncio
async def test_dossier_rejects_text_over_limit_without_truncating(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    monkeypatch.setattr(prd, "MAX_EXPORT_TEXT_CHARS", 50)
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC),
        content="TEXTO-LONGO-" * 20,
    )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 422
    assert "caracteres" in response.json()["detail"]
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "text_limit_exceeded"


@pytest.mark.asyncio
async def test_dossier_rejects_more_than_max_pages(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    monkeypatch.setattr(prd, "MAX_EXPORT_PAGES", 1)
    for index in range(30):
        await _add_evolution(
            db_session,
            patient,
            when=datetime(2026, 3, 1, 9, 0, tzinfo=UTC) + timedelta(days=index),
            content=f"Evolução {index} — " + ("texto clínico " * 20),
        )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.content[:5] != b"%PDF"
    assert "páginas" in response.json()["detail"]
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "page_limit_exceeded"


# --------------------------------------------------------------------------- #
# Limites reais (constantes de produção) e render fora do event loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dossier_rejects_real_text_limit_without_truncating(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    await _add_evolution(
        db_session,
        patient,
        when=datetime(2026, 3, 15, 14, 5, tzinfo=UTC),
        content="TEXTO-LONGO-" * 18_200,  # 200.200 caracteres > 200.000
    )

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "evolutions"]
    )

    assert response.status_code == 422
    assert "200.000 caracteres" in response.json()["detail"]
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "text_limit_exceeded"


@pytest.mark.asyncio
async def test_dossier_rejects_real_dated_record_limit(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    db_session.add_all(
        [
            Session(
                patient_id=patient.id,
                professional_id=patient.professional_id,
                date=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
                duration=50,
                type=f"SESSAO-MASSA-{index}",
                objectives=[],
                notes="",
            )
            for index in range(501)
        ]
    )
    await db_session.commit()

    response = await _post_export(
        api_client, auth_headers, patient, sections=["identification", "sessions"]
    )

    assert response.status_code == 422
    assert "500 registros datados" in response.json()["detail"]
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "record_limit_exceeded"


@pytest.mark.asyncio
async def test_dossier_pdf_never_downloads_attachments(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    """PDF só indexa: nenhum download de original (só o ZIP os inclui)."""
    attachment = await _add_attachment(db_session, patient)

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(attachment.id)],
    )

    assert response.status_code == 200, response.text
    assert attachment.storage_key not in attachment_storage
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "generated"
    assert rows[0].attachment_count == 1


@pytest.mark.asyncio
async def test_dossier_render_runs_outside_the_event_loop(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    import threading

    real_render = prd.render_patient_record_pdf
    observed: dict = {}
    event_loop_thread = threading.current_thread()

    def spy(**kwargs):
        observed["thread"] = threading.current_thread()
        return real_render(**kwargs)

    monkeypatch.setattr(prd, "render_patient_record_pdf", spy)

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 200, response.text
    assert observed["thread"] is not event_loop_thread
    assert response.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_dossier_render_timeout_returns_503_and_audits_failed(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    monkeypatch.setattr(prd, "RENDER_TIMEOUT_SECONDS", 0.0)

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 503
    assert response.content[:5] != b"%PDF"
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "export_timeout"


@pytest.mark.asyncio
async def test_dossier_operation_budget_timeout_returns_503_and_audits_failed(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    """Orçamento total da operação (API 60 s) também corta a resposta.

    O render é desacelerado (thread) e o orçamento é menor que ele: a resposta
    é 503 e a auditoria marca a falha, sem servir PDF parcial.
    """
    import time

    real_render = prd.render_patient_record_pdf

    def slow_render(**kwargs):
        time.sleep(0.5)
        return real_render(**kwargs)

    monkeypatch.setattr(prd, "render_patient_record_pdf", slow_render)
    monkeypatch.setattr(prd, "EXPORT_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(prd, "RENDER_TIMEOUT_SECONDS", 5.0)

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 503
    assert response.content[:5] != b"%PDF"
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "export_timeout"


# --------------------------------------------------------------------------- #
# Seleção de anexos, escopo e falhas
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dossier_attachment_ids_outside_patient_scope_return_404(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    other = Professional(
        email="dono-alheio-dossie@test.com",
        password_hash="x",
        name="Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other)
    await db_session.flush()
    foreign_patient = Patient(
        professional_id=other.id,
        name="Paciente alheio",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign_patient)
    await db_session.commit()
    foreign_attachment = await _add_attachment(db_session, foreign_patient, name="alheio.pdf")

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(foreign_attachment.id)],
    )

    assert response.status_code == 404
    assert "Anexo não encontrado" in response.json()["detail"]
    assert await _audit_rows(db_session, patient.id) == []


@pytest.mark.asyncio
async def test_dossier_rejects_duplicate_attachment_ids(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    attachment = await _add_attachment(db_session, patient)
    attachment_storage[attachment.storage_key] = ATTACHMENT_BODY_MARKER

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(attachment.id), str(attachment.id)],
    )

    assert response.status_code == 422
    assert "duplicatas" in response.json()["detail"]
    assert await _audit_rows(db_session, patient.id) == []


@pytest.mark.asyncio
async def test_dossier_render_failure_returns_503_and_audits_failed(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    def _boom(**_kwargs):
        raise RuntimeError("renderer indisponível")

    monkeypatch.setattr(prd, "render_patient_record_pdf", _boom)

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    rows = await _audit_rows(db_session, patient.id)
    assert rows[0].status == "failed"
    assert rows[0].error_code == "render_failed"


@pytest.mark.asyncio
async def test_dossier_audit_finalize_failure_never_serves_partial_file(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    monkeypatch,
):
    async def _boom(export_id, **kwargs):
        raise RuntimeError("auditoria indisponível")

    monkeypatch.setattr(prd, "finalize_patient_record_export", _boom)

    response = await _post_export(api_client, auth_headers, patient)

    assert response.status_code == 503
    assert response.content[:5] != b"%PDF"
    rows = await _audit_rows(db_session, patient.id)
    assert len(rows) == 1
    assert rows[0].status == "requested"
    assert rows[0].completed_at is None


@pytest.mark.asyncio
async def test_dossier_export_from_other_professional_returns_404(
    api_client,
    db_session,
    patient,
    export_session_factory,
    document_identity,
):
    other = Professional(
        email="token-alheio-dossie@test.com",
        password_hash="x",
        name="Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers={"Authorization": f"Bearer {create_access_token(other.id)}"},
        json=_payload(),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dossier_export_requires_authentication(api_client, patient):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports", json=_payload()
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dossier_rejects_invalid_payload_with_422(
    api_client, auth_headers, patient, export_session_factory, document_identity
):
    response = await _post_export(
        api_client, auth_headers, patient, sections=["goals"]
    )
    assert response.status_code == 422

    response = await _post_export(
        api_client, auth_headers, patient, format="docx"
    )
    assert response.status_code == 422

    response = await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(uuid.uuid4())],
        purpose="curiosity",
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Entitlement: read-only não bloqueia a leitura do próprio acervo
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("subscription_status", ["trial_expired", "past_due", "canceled"])
@pytest.mark.asyncio
async def test_read_only_plan_still_allows_record_export_and_exemption_is_exact(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
    export_session_factory,
    document_identity,
    subscription_status,
):
    from app.core.auth_cookies import ACCESS_COOKIE

    professional.subscription_status = subscription_status
    await db_session.commit()
    cookie = {ACCESS_COOKIE: create_access_token(professional.id)}

    allowed = await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        json=_payload(),
    )
    assert allowed.status_code == 200, allowed.text

    # A exceção é exata: nenhuma outra mutação é liberada.
    other_mutation = await api_client.post(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )
    assert other_mutation.status_code == 403
    extra_path = await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports-extra",
        headers=auth_headers,
        json=_payload(),
    )
    assert extra_path.status_code == 403
    non_uuid = await api_client.post(
        "/api/v1/patients/not-a-uuid/record-exports",
        headers=auth_headers,
        json=_payload(),
    )
    assert non_uuid.status_code == 403

    # GET do histórico nunca é bloqueado por entitlement.
    listed = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports", cookies=cookie
    )
    assert listed.status_code == 200, listed.text


# --------------------------------------------------------------------------- #
# GET /patients/{id}/record-exports — histórico paginado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_record_exports_returns_paginated_camel_case_history(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    attachment = await _add_attachment(db_session, patient)
    attachment_storage[attachment.storage_key] = ATTACHMENT_BODY_MARKER
    await _post_export(
        api_client,
        auth_headers,
        patient,
        sections=["identification", "attachments"],
        attachmentIds=[str(attachment.id)],
    )
    await _post_export(api_client, auth_headers, patient, sections=["identification"])
    summary = await api_client.get(
        f"/api/v1/patients/{patient.id}/export.pdf", headers=auth_headers
    )
    assert summary.status_code == 200, summary.text

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        params={"page": 1, "limit": 20},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "page", "limit"}
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["limit"] == 20
    assert len(body["items"]) == 3
    first = body["items"][0]
    assert set(first) == {
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
    assert uuid.UUID(first["id"])
    assert uuid.UUID(first["actorProfessionalId"])
    assert first["actorProfessionalId"] == str(patient.professional_id)
    kinds = sorted(item["kind"] for item in body["items"])
    assert kinds == ["dossier", "dossier", "summary"]
    assert all(item["status"] == "generated" for item in body["items"])
    # O item do resumo B carrega o tamanho/hash exatos do documento servido.
    summary_item = next(item for item in body["items"] if item["kind"] == "summary")
    assert summary_item["sizeBytes"] == len(summary.content)
    assert summary_item["sha256"] == hashlib.sha256(summary.content).hexdigest()
    assert summary_item["sections"] == ["identification", "goals", "sessions"]
    serialized = response.text.lower()
    for forbidden in ("storage_key", "storagekey", "presign", "http://", "https://", "token"):
        assert forbidden not in serialized, f"histórico vazou {forbidden}"

    second_page = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        params={"page": 2, "limit": 2},
    )
    assert second_page.status_code == 200
    assert second_page.json()["total"] == 3
    assert len(second_page.json()["items"]) == 1

    invalid = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        params={"limit": 101},
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_list_record_exports_is_scoped_to_the_patient(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
    export_session_factory,
    document_identity,
):
    await _post_export(api_client, auth_headers, patient, sections=["identification"])

    other = Professional(
        email="outro-historico@test.com",
        password_hash="x",
        name="Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other)
    await db_session.flush()
    other_patient = Patient(
        professional_id=other.id,
        name="Outro paciente",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(other_patient)
    await db_session.commit()
    foreign_row = PatientRecordExport(
        patient_id=other_patient.id,
        professional_id=other.id,
        kind="dossier",
        format="pdf",
        sections=["identification"],
        purpose="care_continuity",
        status="generated",
        requested_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    db_session.add(foreign_row)
    await db_session.commit()

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    own_rows = await _audit_rows(db_session, patient.id)
    assert [item["id"] for item in body["items"]] == [str(own_rows[0].id)]
    assert str(foreign_row.id) not in response.text

    foreign = await api_client.get(
        f"/api/v1/patients/{other_patient.id}/record-exports", headers=auth_headers
    )
    assert foreign.status_code == 404

    unauthenticated = await api_client.get(
        f"/api/v1/patients/{patient.id}/record-exports"
    )
    assert unauthenticated.status_code == 401
