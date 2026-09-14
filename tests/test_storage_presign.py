"""Unit tests for presigned URL TTL and Content-Disposition."""

from unittest.mock import AsyncMock

import pytest

from app.services.storage import (
    DEFAULT_PRESIGN_EXPIRES,
    StorageService,
    safe_content_disposition_filename,
)


def test_default_presign_expires_at_most_600():
    assert DEFAULT_PRESIGN_EXPIRES <= 600


def test_safe_content_disposition_filename():
    assert safe_content_disposition_filename("patients/x/y/laudo.pdf") == "laudo.pdf"
    assert safe_content_disposition_filename("k", filename='evil"name.pdf') == "evilname.pdf"
    assert safe_content_disposition_filename("k", filename="../../etc/passwd") == "passwd"


@pytest.mark.parametrize(
    ("key", "title", "expected"),
    [
        ("resources/original.pdf", "Cards Intraverbais", "Cards Intraverbais.pdf"),
        ("resources/original.pdf", "CAIXINHA TDAH 2.0", "CAIXINHA TDAH 2.0.pdf"),
        ("resources/original.pdf", "Material.PDF", "Material.PDF"),
        ("resources/original.png", "Imagem", "Imagem.png"),
        ("resources/original.jpeg", "Imagem", "Imagem.jpeg"),
        ("resources/original.webp", "Imagem", "Imagem.webp"),
        ("resources/original.pdf", "A" * 200, "A" * 176 + ".pdf"),
        ("resources/original.pdf", '../../evil"\r\nname', "evilname.pdf"),
    ],
)
def test_download_filename_preserves_stored_extension(key, title, expected):
    assert safe_content_disposition_filename(key, title) == expected


@pytest.mark.asyncio
async def test_presigned_url_params_include_attachment_disposition(monkeypatch):
    service = StorageService()
    mock_client = AsyncMock()
    mock_client.generate_presigned_url = AsyncMock(return_value="https://example.com/signed")

    class _Ctx:
        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(service, "_client", lambda: _Ctx())

    url = await service.presigned_url("patients/p/u/relatorio.pdf", filename="relatorio")
    assert url == "https://example.com/signed"

    kwargs = mock_client.generate_presigned_url.await_args.kwargs
    assert kwargs["ExpiresIn"] == DEFAULT_PRESIGN_EXPIRES
    assert kwargs["ExpiresIn"] <= 600
    params = kwargs["Params"]
    assert params["Key"] == "patients/p/u/relatorio.pdf"
    assert params["ResponseContentDisposition"] == 'attachment; filename="relatorio.pdf"'


@pytest.mark.asyncio
async def test_presigned_url_inline_when_requested(monkeypatch):
    service = StorageService()
    mock_client = AsyncMock()
    mock_client.generate_presigned_url = AsyncMock(return_value="https://example.com/inline")

    class _Ctx:
        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(service, "_client", lambda: _Ctx())

    await service.presigned_url(
        "patients/p/u/foto.png",
        filename="foto.png",
        as_attachment=False,
    )
    params = mock_client.generate_presigned_url.await_args.kwargs["Params"]
    assert params["ResponseContentDisposition"] == 'inline; filename="foto.png"'
