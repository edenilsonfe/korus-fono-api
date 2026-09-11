"""F6 — POST /patients/{id}/record-exports (dossiê ZIP) com manifesto e origem.

O ZIP contém ``prontuario.pdf`` + ``manifest.json`` + ``anexos/{id}-{nome}``
somente com os anexos explicitamente selecionados. Os originais saem intactos
(sem marca d'água) e o manifesto é versionado, sem chaves de storage, presigns
ou tokens. Falha de storage/limite real de bytes não gera pacote parcial.
"""

import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, date, datetime

import pytest
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.attachment import Attachment
from app.models.patient import Patient
from app.models.patient_record_export import PatientRecordExport
from app.models.professional import Professional
from app.services import patient_record_export as prd
from app.services.storage import StorageLimitExceededError, storage_service

PDF_ANEXO_BODY = b"%PDF-1.7 anexo original sem marca"
PNG_ANEXO_BODY = b"\x89PNG\r\n\x1a\n" + b"original-png-bytes" * 3
DOCX_ANEXO_BODY = b"PK\x03\x04docx-nao-selecionado"
EXE_ANEXO_BODY = b"MZ\x90\x00executavel-nao-automatico"


def _payload(**overrides) -> dict:
    payload = {
        "format": "zip",
        "sections": ["identification", "attachments"],
        "purpose": "patient_request",
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
    bodies: dict[str, bytes] = {}

    async def fake_download_limited(key, max_bytes, timeout_seconds=30):
        if key not in bodies:
            raise RuntimeError(f"objeto ausente: {key}")
        body = bodies[key]
        if len(body) > max_bytes:
            raise StorageLimitExceededError("estouro")
        return body, "application/octet-stream"

    monkeypatch.setattr(storage_service, "download_limited", fake_download_limited)
    return bodies


async def _add_attachment(
    db_session, patient, *, name, body, category="relatorio", when=None
):
    key = f"patients/{patient.id}/attachments/{uuid.uuid4().hex}/{name}"
    attachment = Attachment(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        name=name,
        category=category,
        size_bytes=len(body),
        storage_key=key,
        date=when or datetime(2026, 3, 5, 10, 0, tzinfo=UTC),
    )
    db_session.add(attachment)
    await db_session.commit()
    await db_session.refresh(attachment)
    return attachment


async def _audit_row(db_session, patient_id):
    from sqlalchemy import select

    result = await db_session.execute(
        select(PatientRecordExport).where(PatientRecordExport.patient_id == patient_id)
    )
    return result.scalars().one()


def _zip_entries(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None
    return zf


async def _post_zip(api_client, auth_headers, patient, **overrides):
    return await api_client.post(
        f"/api/v1/patients/{patient.id}/record-exports",
        headers=auth_headers,
        json=_payload(**overrides),
    )


@pytest.mark.asyncio
async def test_zip_contains_pdf_manifest_and_selected_originals_unchanged(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    selection = await _add_attachment(
        db_session, patient, name="exame.pdf", body=PDF_ANEXO_BODY
    )
    photo = await _add_attachment(
        db_session, patient, name="foto.png", body=PNG_ANEXO_BODY, category="foto"
    )
    unselected = await _add_attachment(
        db_session, patient, name="laudo.docx", body=DOCX_ANEXO_BODY
    )
    for attachment, body in (
        (selection, PDF_ANEXO_BODY),
        (photo, PNG_ANEXO_BODY),
        (unselected, DOCX_ANEXO_BODY),
    ):
        attachment_storage[attachment.storage_key] = body

    response = await _post_zip(
        api_client,
        auth_headers,
        patient,
        attachmentIds=[str(selection.id), str(photo.id)],
        **{"from": "2026-01-01", "to": "2026-12-31"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    export_id = uuid.UUID(response.headers["x-export-id"])
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="prontuario-{export_id}.zip"'
    )

    with _zip_entries(response.content) as zf:
        names = zf.namelist()
        assert names[0] == "prontuario.pdf"
        assert "manifest.json" in names
        assert f"anexos/{selection.id}-exame.pdf" in names
        assert f"anexos/{photo.id}-foto.png" in names
        assert len(names) == 4, f"entradas inesperadas no ZIP: {names}"
        assert not any(str(unselected.id) in name for name in names)
        # Originais intactos: sem marca d'água, sem transformação.
        assert zf.read(f"anexos/{selection.id}-exame.pdf") == PDF_ANEXO_BODY
        assert zf.read(f"anexos/{photo.id}-foto.png") == PNG_ANEXO_BODY

        pdf_bytes = zf.read("prontuario.pdf")
        reader = PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) >= 1
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "CONFIDENCIAL" in pdf_text
        assert str(export_id) in pdf_text
        # Índice de anexos: nome listado, binário não embutido.
        assert "exame.pdf" in pdf_text
        assert "foto.png" in pdf_text

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert manifest["manifestVersion"] == 1
    assert manifest["exportId"] == str(export_id)
    assert manifest["kind"] == "dossier"
    assert manifest["format"] == "zip"
    assert manifest["sections"] == ["identification", "attachments"]
    assert manifest["purpose"] == "patient_request"
    assert manifest["from"] == "2026-01-01"
    assert manifest["to"] == "2026-12-31"
    assert datetime.fromisoformat(manifest["generatedAt"])
    assert manifest["counts"] == {"attachments": 2}
    assert manifest["document"]["entry"] == "prontuario.pdf"
    assert manifest["document"]["sizeBytes"] == len(pdf_bytes)
    assert manifest["document"]["sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
    files = {entry["attachmentId"]: entry for entry in manifest["attachments"]}
    assert set(files) == {str(selection.id), str(photo.id)}
    assert files[str(selection.id)]["name"] == "exame.pdf"
    assert files[str(selection.id)]["sizeBytes"] == len(PDF_ANEXO_BODY)
    assert (
        files[str(selection.id)]["sha256"] == hashlib.sha256(PDF_ANEXO_BODY).hexdigest()
    )
    assert files[str(photo.id)]["sha256"] == hashlib.sha256(PNG_ANEXO_BODY).hexdigest()
    assert "originais" in manifest["notice"]

    row = await _audit_row(db_session, patient.id)
    assert row.status == "generated"
    assert row.kind == "dossier"
    assert row.format == "zip"
    assert row.attachment_count == 2
    assert row.size_bytes == len(response.content)
    assert row.sha256 == hashlib.sha256(response.content).hexdigest()


@pytest.mark.asyncio
async def test_zip_manifest_never_exposes_storage_keys_urls_or_tokens(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    attachment = await _add_attachment(
        db_session, patient, name="segredo.pdf", body=PDF_ANEXO_BODY
    )
    attachment_storage[attachment.storage_key] = PDF_ANEXO_BODY

    response = await _post_zip(
        api_client, auth_headers, patient, attachmentIds=[str(attachment.id)]
    )

    assert response.status_code == 200, response.text
    with _zip_entries(response.content) as zf:
        manifest_text = zf.read("manifest.json").decode("utf-8")
        names = zf.namelist()

    assert attachment.storage_key not in manifest_text
    for forbidden in ("s3", "presign", "X-Amz", "http://", "https://", "token", "key"):
        assert forbidden.lower() not in manifest_text.lower(), f"manifesto vazou {forbidden}"
    assert all(".." not in name and not name.startswith("/") for name in names)


@pytest.mark.asyncio
async def test_zip_entry_names_sanitize_traversal_attempts(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    traversal = await _add_attachment(
        db_session, patient, name="../../etc/passwd.pdf", body=PDF_ANEXO_BODY
    )
    backslash = await _add_attachment(
        db_session, patient, name="..\\..\\windows\\evil.pdf", body=PNG_ANEXO_BODY
    )
    attachment_storage[traversal.storage_key] = PDF_ANEXO_BODY
    attachment_storage[backslash.storage_key] = PNG_ANEXO_BODY

    response = await _post_zip(
        api_client,
        auth_headers,
        patient,
        attachmentIds=[str(traversal.id), str(backslash.id)],
    )

    assert response.status_code == 200, response.text
    with _zip_entries(response.content) as zf:
        names = zf.namelist()
        manifest_text = zf.read("manifest.json").decode("utf-8")

    for name in names:
        assert not name.startswith("/")
        assert ".." not in name.split("/")
        assert "\\" not in name
    assert f"anexos/{traversal.id}-passwd.pdf" in names
    assert f"anexos/{backslash.id}-evil.pdf" in names
    assert ".." not in manifest_text
    assert "\\" not in manifest_text


@pytest.mark.asyncio
async def test_zip_does_not_include_unselected_office_or_executable_files(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    selected = await _add_attachment(
        db_session, patient, name="selecionado.pdf", body=PDF_ANEXO_BODY
    )
    office = await _add_attachment(
        db_session, patient, name="contrato.docx", body=DOCX_ANEXO_BODY, category="relatorio"
    )
    executable = await _add_attachment(
        db_session, patient, name="evidencia-bateria.exe", body=EXE_ANEXO_BODY
    )
    attachment_storage[selected.storage_key] = PDF_ANEXO_BODY
    attachment_storage[office.storage_key] = DOCX_ANEXO_BODY
    attachment_storage[executable.storage_key] = EXE_ANEXO_BODY

    response = await _post_zip(
        api_client, auth_headers, patient, attachmentIds=[str(selected.id)]
    )

    assert response.status_code == 200, response.text
    with _zip_entries(response.content) as zf:
        names = zf.namelist()

    assert names == [
        "prontuario.pdf",
        f"anexos/{selected.id}-selecionado.pdf",
        "manifest.json",
    ]


@pytest.mark.asyncio
async def test_zip_with_empty_selection_has_no_original_files(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    """``attachmentIds: []`` nunca significa "todos"."""
    existing = await _add_attachment(
        db_session, patient, name="nao-selecionado.pdf", body=PDF_ANEXO_BODY
    )
    attachment_storage[existing.storage_key] = PDF_ANEXO_BODY

    response = await _post_zip(api_client, auth_headers, patient, attachmentIds=[])

    assert response.status_code == 200, response.text
    with _zip_entries(response.content) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert names == ["prontuario.pdf", "manifest.json"]
    assert manifest["attachments"] == []
    assert manifest["counts"] == {"attachments": 0}


@pytest.mark.asyncio
async def test_zip_duplicate_names_get_distinct_entries(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    first = await _add_attachment(
        db_session, patient, name="relatorio.pdf", body=PDF_ANEXO_BODY
    )
    second = await _add_attachment(
        db_session, patient, name="relatorio.pdf", body=PNG_ANEXO_BODY
    )
    attachment_storage[first.storage_key] = PDF_ANEXO_BODY
    attachment_storage[second.storage_key] = PNG_ANEXO_BODY

    response = await _post_zip(
        api_client,
        auth_headers,
        patient,
        attachmentIds=[str(first.id), str(second.id)],
    )

    assert response.status_code == 200, response.text
    with _zip_entries(response.content) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert f"anexos/{first.id}-relatorio.pdf" in names
    assert f"anexos/{second.id}-relatorio.pdf" in names
    assert len(names) == len(set(names))
    assert len(manifest["attachments"]) == 2


@pytest.mark.asyncio
async def test_zip_rejects_real_bytes_over_aggregate_limit(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
    monkeypatch,
):
    monkeypatch.setattr(prd, "MAX_EXPORT_ATTACHMENT_BYTES", 64)
    honest = await _add_attachment(db_session, patient, name="ok.pdf", body=b"a" * 32)
    liar = await _add_attachment(db_session, patient, name="mentiroso.pdf", body=b"b" * 8)
    attachment_storage[honest.storage_key] = b"a" * 32
    # Declara 8 bytes, entrega 100: o orçamento real mede o corpo.
    attachment_storage[liar.storage_key] = b"b" * 100

    response = await _post_zip(
        api_client,
        auth_headers,
        patient,
        attachmentIds=[str(honest.id), str(liar.id)],
    )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.content[:2] != b"PK"
    row = await _audit_row(db_session, patient.id)
    assert row.status == "failed"
    assert row.error_code == "attachment_limit_exceeded"
    assert row.size_bytes is None


@pytest.mark.asyncio
async def test_zip_storage_failure_returns_503_without_partial_file(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    attachment = await _add_attachment(
        db_session, patient, name="sumido.pdf", body=PDF_ANEXO_BODY
    )
    # storage mockado sem o objeto cadastrado -> RuntimeError do download.

    response = await _post_zip(
        api_client, auth_headers, patient, attachmentIds=[str(attachment.id)]
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.content[:2] != b"PK"
    assert "anexos" in response.json()["detail"]
    row = await _audit_row(db_session, patient.id)
    assert row.status == "failed"
    assert row.error_code == "storage_unavailable"


@pytest.mark.asyncio
async def test_zip_missing_attachment_row_is_never_silently_omitted(
    api_client,
    auth_headers,
    db_session,
    patient,
    export_session_factory,
    document_identity,
    attachment_storage,
):
    attachment = await _add_attachment(
        db_session, patient, name="removido.pdf", body=PDF_ANEXO_BODY
    )
    attachment_storage[attachment.storage_key] = PDF_ANEXO_BODY
    await db_session.delete(attachment)
    await db_session.commit()

    response = await _post_zip(
        api_client, auth_headers, patient, attachmentIds=[str(attachment.id)]
    )

    assert response.status_code == 404
    assert response.content[:2] != b"PK"


@pytest.mark.asyncio
async def test_zip_export_of_foreign_patient_returns_404(
    api_client, auth_headers, db_session, export_session_factory, document_identity
):
    other = Professional(
        email="dono-zip-alheio@test.com",
        password_hash="x",
        name="Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other)
    await db_session.flush()
    foreign = Patient(
        professional_id=other.id,
        name="Paciente alheio",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign)
    await db_session.commit()

    response = await api_client.post(
        f"/api/v1/patients/{foreign.id}/record-exports",
        headers=auth_headers,
        json=_payload(),
    )

    assert response.status_code == 404
