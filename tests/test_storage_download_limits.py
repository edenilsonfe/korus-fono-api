"""F6 — ``StorageService.download_limited``: orçamento real de bytes e deadline.

O ``ContentLength`` é só uma checagem antecipada: o corpo é lido em blocos e
medido de verdade, o stream é sempre fechado e o estouro vira
``StorageLimitExceededError`` — nunca download parcial silencioso. O deadline
(timeout) também está coberto; ``download`` (usado por F1/F2/anexos) mantém a
assinatura atual.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services.storage import (
    DOWNLOAD_CHUNK_SIZE,
    StorageLimitExceededError,
    storage_service,
)

KEY = "patients/abc/attachments/exame.pdf"


class FakeBody:
    def __init__(self, chunks=(), *, hang=False, error=None):
        self._chunks = list(chunks)
        self.hang = hang
        self.error = error
        self.closed = False
        self.reads = 0
        self.read_sizes: list[int] = []

    async def read(self, size=-1):
        self.reads += 1
        self.read_sizes.append(size)
        if self.hang:
            await asyncio.sleep(30)
        if self.error is not None:
            raise self.error
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(
        self, body, *, content_length=None, content_type="application/pdf", error=None
    ):
        self.body = body
        self.content_length = content_length
        self.content_type = content_type
        self.error = error
        self.requests: list[tuple[str, str]] = []

    async def get_object(self, *, Bucket, Key):
        self.requests.append((Bucket, Key))
        if self.error is not None:
            raise self.error
        obj: dict = {"Body": self.body}
        if self.content_length is not None:
            obj["ContentLength"] = self.content_length
        if self.content_type is not None:
            obj["ContentType"] = self.content_type
        return obj


def _patch_client(monkeypatch, client):
    @asynccontextmanager
    async def fake_client():
        yield client

    monkeypatch.setattr(storage_service, "_client", fake_client)
    return client


async def test_download_limited_returns_body_and_content_type(monkeypatch):
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody([b"abc", b"def"]), content_length=6)
    )

    body, content_type = await storage_service.download_limited(
        KEY, max_bytes=10, timeout_seconds=5
    )

    assert body == b"abcdef"
    assert content_type == "application/pdf"
    assert client.requests == [(storage_service.settings.s3_bucket, KEY)]
    assert client.body.closed is True


async def test_download_limited_accepts_consumption_exactly_at_the_limit(monkeypatch):
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody([b"a" * 5, b"b" * 5]), content_length=10)
    )

    body, _ = await storage_service.download_limited(
        KEY, max_bytes=10, timeout_seconds=5
    )

    assert body == b"a" * 5 + b"b" * 5
    assert len(body) == 10
    assert client.body.closed is True


async def test_download_limited_measures_real_bytes_beyond_content_length(monkeypatch):
    """ContentLength mente para baixo; os bytes reais estouram o orçamento."""
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody([b"x" * 12]), content_length=4)
    )

    with pytest.raises(StorageLimitExceededError):
        await storage_service.download_limited(KEY, max_bytes=8, timeout_seconds=5)

    assert client.body.closed is True


async def test_download_limited_rejects_declared_oversize_without_reading(monkeypatch):
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody([b"x" * 100]), content_length=100)
    )

    with pytest.raises(StorageLimitExceededError):
        await storage_service.download_limited(KEY, max_bytes=10, timeout_seconds=5)

    assert client.body.reads == 0
    assert client.body.closed is True


async def test_download_limited_enforces_deadline_and_closes_body(monkeypatch):
    client = _patch_client(monkeypatch, FakeClient(FakeBody([b"x"], hang=True)))

    with pytest.raises(TimeoutError):
        await storage_service.download_limited(KEY, max_bytes=10, timeout_seconds=0.05)

    assert client.body.closed is True


async def test_download_limited_closes_body_when_read_fails(monkeypatch):
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody(error=RuntimeError("stream quebrado")))
    )

    with pytest.raises(RuntimeError):
        await storage_service.download_limited(KEY, max_bytes=10, timeout_seconds=5)

    assert client.body.closed is True


async def test_download_limited_reads_in_bounded_chunks(monkeypatch):
    payload = b"z" * (DOWNLOAD_CHUNK_SIZE + 17)
    client = _patch_client(
        monkeypatch,
        FakeClient(
            FakeBody([payload[:DOWNLOAD_CHUNK_SIZE], payload[DOWNLOAD_CHUNK_SIZE:]]),
            content_length=len(payload),
        ),
    )

    body, _ = await storage_service.download_limited(
        KEY, max_bytes=len(payload), timeout_seconds=5
    )

    assert body == payload
    assert client.body.read_sizes
    assert all(size == DOWNLOAD_CHUNK_SIZE for size in client.body.read_sizes)


async def test_download_limited_reaches_storage_error(monkeypatch):
    _patch_client(monkeypatch, FakeClient(FakeBody(), error=RuntimeError("sem S3")))

    with pytest.raises(RuntimeError, match="sem S3"):
        await storage_service.download_limited(KEY, max_bytes=10, timeout_seconds=5)


async def test_existing_download_keeps_single_argument_signature(monkeypatch):
    """F1/F2/anexos continuam chamando ``download(key)`` sem limite."""
    client = _patch_client(
        monkeypatch, FakeClient(FakeBody([b"%PDF-1.7 anexo"]), content_length=13)
    )

    body, content_type = await storage_service.download(KEY)

    assert body == b"%PDF-1.7 anexo"
    assert content_type == "application/pdf"
    assert client.requests == [(storage_service.settings.s3_bucket, KEY)]
