"""Professional document identity: branding upload, URLs and export rendering."""

import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PillowImage
from docx import Document
from sqlalchemy import select

from app.models.professional import Professional
from app.services import professional_branding
from app.services.professional_branding import (
    MAX_BRANDING_BYTES,
    build_document_identity,
    validate_branding_image,
)
from app.services.report_export import (
    DocumentIdentity,
    export_docx,
    export_pdf,
    export_txt,
)

# 1x1 black pixel PNG.
_png_buffer = io.BytesIO()
PillowImage.new("RGB", (1, 1), "black").save(_png_buffer, format="PNG")
PNG_BYTES = _png_buffer.getvalue()
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@pytest.fixture
def storage_mocks(monkeypatch):
    upload = AsyncMock(return_value="professionals/test/branding/logo.png")
    presigned = AsyncMock(return_value="https://storage.local/branding.png")
    download = AsyncMock(return_value=(PNG_BYTES, "image/png"))
    monkeypatch.setattr(professional_branding.storage_service, "upload", upload)
    monkeypatch.setattr(professional_branding.storage_service, "presigned_url", presigned)
    monkeypatch.setattr(professional_branding.storage_service, "download", download)
    return upload, presigned, download


async def test_branding_starts_empty(api_client, auth_headers):
    response = await api_client.get("/api/v1/me/branding", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"logoUrl": None, "signatureUrl": None}


async def test_upload_logo_persists_key_and_returns_url(
    api_client, auth_headers, db_session, professional, storage_mocks
):
    upload, _presigned, _download = storage_mocks
    response = await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["logoUrl"] == "https://storage.local/branding.png"
    upload.assert_awaited_once()
    key, body, content_type = upload.await_args.args
    assert key == f"professionals/{professional.id}/branding/logo.png"
    assert body == PNG_BYTES
    assert content_type == "image/png"

    stored = await db_session.scalar(
        select(Professional.branding_logo_key).where(Professional.id == professional.id)
    )
    assert stored == key


async def test_upload_signature_accepts_jpeg(
    api_client, auth_headers, db_session, professional, storage_mocks
):
    upload, _presigned, _download = storage_mocks
    response = await api_client.post(
        "/api/v1/me/branding/signature",
        headers=auth_headers,
        files={"file": ("assinatura.jpg", JPEG_BYTES, "image/jpeg")},
    )
    assert response.status_code == 200
    key = upload.await_args.args[0]
    assert key.endswith("/branding/signature.jpg")


async def test_upload_rejects_unsupported_type(api_client, auth_headers, storage_mocks):
    response = await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.gif", b"GIF89a" + b"\x00" * 10, "image/gif")},
    )
    assert response.status_code == 400
    assert "PNG" in response.json()["detail"]


async def test_upload_rejects_sniff_mismatch(api_client, auth_headers, storage_mocks):
    response = await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.png", JPEG_BYTES, "image/png")},
    )
    assert response.status_code == 400


async def test_upload_rejects_empty_and_oversized(api_client, auth_headers, storage_mocks):
    empty = await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.png", b"", "image/png")},
    )
    assert empty.status_code == 400

    oversized_body = PNG_BYTES + b"\x00" * (MAX_BRANDING_BYTES + 1)
    oversized = await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.png", oversized_body, "image/png")},
    )
    assert oversized.status_code == 400
    assert "2 MB" in oversized.json()["detail"]


async def test_upload_rejects_unknown_asset(api_client, auth_headers, storage_mocks):
    response = await api_client.post(
        "/api/v1/me/branding/stamp",
        headers=auth_headers,
        files={"file": ("stamp.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 422


async def test_delete_branding_clears_key(
    api_client, auth_headers, db_session, professional, storage_mocks
):
    await api_client.post(
        "/api/v1/me/branding/logo",
        headers=auth_headers,
        files={"file": ("logo.png", PNG_BYTES, "image/png")},
    )
    response = await api_client.delete("/api/v1/me/branding/logo", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["logoUrl"] is None
    stored = await db_session.scalar(
        select(Professional.branding_logo_key).where(Professional.id == professional.id)
    )
    assert stored is None


async def test_build_document_identity_reads_storage(professional, storage_mocks):
    professional.branding_logo_key = "professionals/x/branding/logo.png"
    professional.branding_signature_key = None
    _upload, _presigned, download = storage_mocks
    identity = await build_document_identity(professional)
    assert identity.professional_name == professional.name
    assert identity.council == "CREFITO"
    assert identity.logo_bytes == PNG_BYTES
    assert identity.signature_bytes is None
    download.assert_awaited_once()


async def test_build_document_identity_without_keys_skips_storage(
    professional, storage_mocks
):
    _upload, _presigned, download = storage_mocks
    identity = await build_document_identity(professional)
    assert identity.logo_bytes is None
    download.assert_not_awaited()


def test_validate_branding_image_accepts_png_and_jpeg():
    assert validate_branding_image(content_type="image/png", body=PNG_BYTES) == "image/png"
    assert (
        validate_branding_image(content_type="image/jpeg; charset=binary", body=JPEG_BYTES)
        == "image/jpeg"
    )


def test_export_txt_carries_identity():
    from datetime import date

    identity = DocumentIdentity(
        professional_name="Dra. Teste", council="CREFITO 12345", issued_at=date(2026, 9, 10)
    )
    data = export_txt("clinico", "João Silva", date(2026, 9, 1), "## Seção\nConteúdo", identity)
    text = data.decode("utf-8")
    assert "Profissional: Dra. Teste — CREFITO 12345" in text
    assert "Emitido em 2026-09-10" in text


def test_export_txt_without_identity_keeps_previous_format():
    from datetime import date

    data = export_txt("clinico", "João Silva", date(2026, 9, 1), "Conteúdo")
    text = data.decode("utf-8")
    assert "Profissional:" not in text
    assert "Emitido em" not in text


def test_export_docx_embeds_logo_signature_and_name():
    from datetime import date

    identity = DocumentIdentity(
        professional_name="Dra. Teste",
        council="CREFITO 12345",
        issued_at=date(2026, 9, 10),
        logo_bytes=PNG_BYTES,
        signature_bytes=PNG_BYTES,
    )
    data = export_docx("pais", "João Silva", date(2026, 9, 1), "## Resumo\nTudo bem.", identity)
    doc = Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs]
    assert any("Profissional: Dra. Teste — CREFITO 12345" in p for p in paragraphs)
    assert any("Emitido em 2026-09-10" in p for p in paragraphs)
    assert len(doc.inline_shapes) == 2


@pytest.mark.parametrize("asset", ["logo_bytes", "signature_bytes"])
def test_export_pdf_with_identity_and_images_builds(asset):
    from datetime import date

    identity = DocumentIdentity(
        professional_name="Dra. Teste",
        council="CREFITO 12345",
        issued_at=date(2026, 9, 10),
        **{asset: PNG_BYTES},
    )
    data = export_pdf("escolar", "João Silva", date(2026, 9, 1), "## Seção\nConteúdo", identity)
    assert data.startswith(b"%PDF")
    assert b"/Subtype /Image" in data


def test_export_pdf_survives_broken_image_bytes():
    from datetime import date

    identity = DocumentIdentity(
        professional_name="Dra. Teste",
        logo_bytes=b"not-an-image",
        signature_bytes=b"also-broken",
    )
    data = export_pdf("clinico", "João Silva", date(2026, 9, 1), "Conteúdo", identity)
    assert data.startswith(b"%PDF")
