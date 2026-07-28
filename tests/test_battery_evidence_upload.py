"""Unit tests for chunked battery evidence upload size cap + sniff."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.battery_evidence_service import BatteryEvidenceService, EVIDENCE_SIZE_LIMITS

_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 28
_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 24
_PDF = b"%PDF-1.4\ndata"


class _ChunkedUploadFile:
    def __init__(
        self,
        data: bytes,
        *,
        content_type: str = "image/jpeg",
        filename: str = "photo.jpg",
    ):
        self._data = data
        self._pos = 0
        self.content_type = content_type
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _make_service() -> tuple[BatteryEvidenceService, MagicMock, MagicMock]:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)

    db.refresh = AsyncMock(side_effect=_refresh)

    service = BatteryEvidenceService(db)
    battery = MagicMock()
    battery.patient_id = uuid4()
    service._load_battery = AsyncMock(return_value=battery)
    return service, db, battery


@pytest.mark.asyncio
async def test_upload_under_limit_calls_storage(monkeypatch):
    monkeypatch.setitem(EVIDENCE_SIZE_LIMITS, "photo", 64)
    service, _db, battery = _make_service()
    body = _JPEG
    file = _ChunkedUploadFile(body)

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.make_key.return_value = "patients/key/photo.jpg"
        storage.upload = AsyncMock()
        storage.presigned_url = AsyncMock(return_value="https://example.com/photo.jpg")

        result = await service.upload_evidence(
            uuid4(),
            professional_id=uuid4(),
            file=file,
            kind="photo",
        )

        storage.upload.assert_awaited_once()
        assert storage.upload.await_args.args[1] == body
        assert result["kind"] == "photo"
        assert result["url"] == "https://example.com/photo.jpg"
        storage.make_key.assert_called_once_with(battery.patient_id, "photo.jpg")


@pytest.mark.asyncio
async def test_upload_over_limit_returns_413_and_skips_storage(monkeypatch):
    monkeypatch.setitem(EVIDENCE_SIZE_LIMITS, "photo", 64)
    service, _db, _battery = _make_service()
    file = _ChunkedUploadFile(b"x" * 65)

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.upload = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await service.upload_evidence(
                uuid4(),
                professional_id=uuid4(),
                file=file,
                kind="photo",
            )

        assert exc_info.value.status_code == 413
        assert "excede limite" in exc_info.value.detail
        storage.upload.assert_not_called()
        storage.make_key.assert_not_called()


@pytest.mark.asyncio
async def test_upload_infers_content_type_when_empty(monkeypatch):
    monkeypatch.setitem(EVIDENCE_SIZE_LIMITS, "photo", 64)
    service, _db, _battery = _make_service()
    file = _ChunkedUploadFile(_JPEG, content_type="", filename="photo.jpg")

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.make_key.return_value = "patients/key/photo.jpg"
        storage.upload = AsyncMock()
        storage.presigned_url = AsyncMock(return_value="https://example.com/photo.jpg")

        result = await service.upload_evidence(
            uuid4(),
            professional_id=uuid4(),
            file=file,
            kind="photo",
        )

        assert result["kind"] == "photo"
        storage.upload.assert_awaited_once()
        assert storage.upload.await_args.args[2] == "image/jpeg"


@pytest.mark.asyncio
async def test_upload_rejects_svg_content_type():
    service, _db, _battery = _make_service()
    file = _ChunkedUploadFile(
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        content_type="image/svg+xml",
        filename="x.svg",
    )

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.upload = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await service.upload_evidence(
                uuid4(),
                professional_id=uuid4(),
                file=file,
                kind="photo",
            )
        assert exc_info.value.status_code == 400
        storage.upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_rejects_magic_mismatch():
    service, _db, _battery = _make_service()
    file = _ChunkedUploadFile(_PDF, content_type="image/png", filename="fake.png")

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.upload = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await service.upload_evidence(
                uuid4(),
                professional_id=uuid4(),
                file=file,
                kind="photo",
            )
        assert exc_info.value.status_code == 400
        assert "não corresponde" in exc_info.value.detail
        storage.upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_sanitizes_path_traversal_filename(monkeypatch):
    monkeypatch.setitem(EVIDENCE_SIZE_LIMITS, "photo", 64)
    service, _db, battery = _make_service()
    file = _ChunkedUploadFile(_PNG, content_type="image/png", filename="../../etc/passwd.png")

    with patch("app.services.battery_evidence_service.storage_service") as storage:
        storage.make_key.return_value = "patients/key/passwd.png"
        storage.upload = AsyncMock()
        storage.presigned_url = AsyncMock(return_value="https://example.com/passwd.png")

        await service.upload_evidence(
            uuid4(),
            professional_id=uuid4(),
            file=file,
            kind="photo",
        )

        storage.make_key.assert_called_once_with(battery.patient_id, "passwd.png")
        added_attachment = next(
            call.args[0]
            for call in service.db.add.call_args_list
            if getattr(call.args[0], "name", None) == "passwd.png"
        )
        assert added_attachment.name == "passwd.png"
