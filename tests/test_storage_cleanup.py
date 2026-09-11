"""F17/4.3 — janitor de blobs: tolerância a ausente, backoff e lote limitado.

Cobre ``StorageService.delete`` (objeto ausente = sucesso; chave exata; nunca
lote/prefixo), a reserva committada antes do upload, a revalidação de
referência viva (Resource/anexo/branding/foto F16 — nunca apaga asset legado
de F1) e o janitor: retentativa com backoff, falha visível após o limite,
diagnóstico sanitizado (sem chave crua nos logs/erros) e lote limitado a 100.
SQLite em memória com tabelas curadas; storage sempre stub local.
"""

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.models.attachment import Attachment
from app.models.home_program import (
    HomeProgram,
    HomeProgramCheckIn,
    HomeProgramPhoto,
    HomeProgramTask,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.storage_cleanup import (
    STORAGE_CLEANUP_DELETED,
    STORAGE_CLEANUP_FAILED,
    STORAGE_CLEANUP_KEPT,
    STORAGE_CLEANUP_PENDING,
    STORAGE_CLEANUP_RESOLVED,
    StorageCleanupTask,
)
from app.services import storage_cleanup_service
from app.services.storage import StorageService
from app.services.storage_cleanup_service import (
    MAX_CLEANUP_ATTEMPTS,
    STORAGE_CLEANUP_BATCH_LIMIT,
    cleanup_backoff,
    redact_storage_key,
    reserve_storage_cleanup,
    resolve_storage_cleanup,
    run_storage_cleanup,
    sanitize_storage_error,
    storage_key_in_use,
)

WAIT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FakeMissingKeyError(Exception):
    """Erro no formato do botocore para objeto/prefixo ausente (NoSuchKey/404)."""

    def __init__(self, key: str) -> None:
        super().__init__(
            "An error occurred (NoSuchKey) when calling the DeleteObject "
            f'operation: Key "{key}" does not exist'
        )
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": "404"},
        }


class _ClientCtx:
    def __init__(self, client) -> None:
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *args):
        return None


def _recording_delete(calls: list[str]):
    async def fake_delete(key: str) -> None:
        calls.append(key)

    return fake_delete


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Patient.__table__,
                    Resource.__table__,
                    Attachment.__table__,
                    StorageCleanupTask.__table__,
                    HomeProgram.__table__,
                    HomeProgramTask.__table__,
                    HomeProgramCheckIn.__table__,
                    HomeProgramPhoto.__table__,
                ],
            )
        )
    return engine


@pytest.fixture
async def session():
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _task(
    db: AsyncSession,
    key: str,
    *,
    status: str = STORAGE_CLEANUP_PENDING,
    not_before: datetime | None = None,
) -> StorageCleanupTask:
    task = StorageCleanupTask(
        storage_key=key,
        status=status,
        reason="resource_create",
        not_before=not_before or WAIT - timedelta(minutes=1),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# StorageService.delete — tolerável, chave exata, nunca em lote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_treats_missing_object_as_success(monkeypatch):
    service = StorageService()
    client = AsyncMock()
    client.delete_object = AsyncMock(side_effect=FakeMissingKeyError("resources/x/op/f.pdf"))
    monkeypatch.setattr(service, "_client", lambda: _ClientCtx(client))

    await service.delete("resources/00000000-0000-0000-0000-000000000001/op/f.pdf")

    client.delete_object.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_uses_exact_key_and_never_batches(monkeypatch):
    service = StorageService()
    client = AsyncMock()
    monkeypatch.setattr(service, "_client", lambda: _ClientCtx(client))

    await service.delete("resources/00000000-0000-0000-0000-000000000001/op/f.pdf")

    kwargs = client.delete_object.await_args.kwargs
    assert kwargs["Key"] == "resources/00000000-0000-0000-0000-000000000001/op/f.pdf"
    assert kwargs["Bucket"] == service.settings.s3_bucket
    client.delete_objects.assert_not_called()
    client.list_objects_v2.assert_not_called()
    client.delete_bucket.assert_not_called()


@pytest.mark.asyncio
async def test_delete_propagates_real_failures(monkeypatch):
    service = StorageService()
    client = AsyncMock()
    client.delete_object = AsyncMock(side_effect=RuntimeError("storage fora do ar"))
    monkeypatch.setattr(service, "_client", lambda: _ClientCtx(client))

    with pytest.raises(RuntimeError):
        await service.delete("resources/00000000-0000-0000-0000-000000000001/op/f.pdf")


@pytest.mark.asyncio
async def test_delete_rejects_empty_or_prefix_keys():
    service = StorageService()
    for bad in ("", "   ", "resources/", "resources/abc/op/", "/resources/abc/f.pdf"):
        with pytest.raises(ValueError):
            await service.delete(bad)


# ---------------------------------------------------------------------------
# Reserva antes do upload e retirada na associação
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_commits_pending_task_that_survives_rollback(session):
    task = await reserve_storage_cleanup(
        session,
        "resources/00000000-0000-0000-0000-000000000001/op/f.pdf",
        reason="resource_create",
    )

    assert task.status == STORAGE_CLEANUP_PENDING
    assert task.attempts == 0
    assert task.not_before is not None

    # Rollback posterior não remove a reserva: ela foi committada antes do
    # upload, então sobrevive à queda/abort do processo.
    await session.rollback()
    row = await session.scalar(
        select(StorageCleanupTask).where(StorageCleanupTask.id == task.id)
    )
    assert row is not None and row.status == STORAGE_CLEANUP_PENDING


def test_resolve_storage_cleanup_only_withdraws_pending_reservation():
    pending = StorageCleanupTask(storage_key="resources/x/op/f.pdf", status=STORAGE_CLEANUP_PENDING)
    resolve_storage_cleanup(pending)
    assert pending.status == STORAGE_CLEANUP_RESOLVED

    failed = StorageCleanupTask(storage_key="resources/x/op/f.pdf", status=STORAGE_CLEANUP_FAILED)
    resolve_storage_cleanup(failed)
    assert failed.status == STORAGE_CLEANUP_FAILED
    resolve_storage_cleanup(None)


# ---------------------------------------------------------------------------
# Janitor — órfão, referência viva, backoff, lote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_janitor_deletes_orphan_and_marks_deleted(session, monkeypatch):
    key = "resources/00000000-0000-0000-0000-000000000001/op/orphan.pdf"
    task = await _task(session, key)
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary == {"selected": 1, "deleted": 1, "kept": 0, "retried": 0, "failed": 0}
    assert calls == [key]
    await session.refresh(task)
    assert task.status == STORAGE_CLEANUP_DELETED


@pytest.mark.asyncio
async def test_janitor_counts_missing_object_as_deleted(session, monkeypatch):
    """Ausente no storage = sucesso também de ponta a ponta (delete tolerante)."""
    key = "resources/00000000-0000-0000-0000-000000000001/op/gone.pdf"
    task = await _task(session, key)
    service = StorageService()
    client = AsyncMock()
    client.delete_object = AsyncMock(side_effect=FakeMissingKeyError(key))
    monkeypatch.setattr(service, "_client", lambda: _ClientCtx(client))
    monkeypatch.setattr(storage_cleanup_service, "storage_service", service)

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["deleted"] == 1 and summary["failed"] == 0
    await session.refresh(task)
    assert task.status == STORAGE_CLEANUP_DELETED
    assert task.attempts == 0


@pytest.mark.asyncio
async def test_janitor_never_removes_blob_referenced_by_resource(session, monkeypatch):
    key = "resources/00000000-0000-0000-0000-000000000002/op/assoc.pdf"
    session.add(
        Resource(
            owner_professional_id=None,
            title="Material associado",
            description="",
            categories=[],
            format="PDF",
            file_size_bytes=1,
            storage_key=key,
            content_type="application/pdf",
        )
    )
    await session.commit()
    task = await _task(session, key)
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["kept"] == 1 and summary["deleted"] == 0
    assert calls == []
    await session.refresh(task)
    assert task.status == STORAGE_CLEANUP_KEPT


@pytest.mark.asyncio
async def test_janitor_preserves_legacy_attachment_and_branding_assets(session, monkeypatch):
    """Assets de F1/branding nunca são registrados — e se estiverem, ficam."""
    attachment_key = "patients/00000000-0000-0000-0000-000000000003/op/anexo.pdf"
    branding_key = "branding/logo-empresa.png"
    session.add(
        Attachment(
            patient_id=uuid.uuid4(),
            professional_id=uuid.uuid4(),
            name="anexo.pdf",
            category="laudo",
            size_bytes=1,
            storage_key=attachment_key,
            date=WAIT,
        )
    )
    session.add(
        Professional(
            email="branding@x.com",
            password_hash="x",
            name="Dona de logo",
            branding_logo_key=branding_key,
        )
    )
    await session.commit()
    attachment_task = await _task(session, attachment_key)
    branding_task = await _task(session, branding_key)
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["kept"] == 2 and calls == []
    await session.refresh(attachment_task)
    await session.refresh(branding_task)
    assert attachment_task.status == STORAGE_CLEANUP_KEPT
    assert branding_task.status == STORAGE_CLEANUP_KEPT
    assert await storage_key_in_use(session, attachment_key) is True
    assert await storage_key_in_use(session, branding_key) is True
    assert await storage_key_in_use(session, "resources/orfao.pdf") is False


async def _home_photo(
    db: AsyncSession, key: str, *, status: str = "ready"
) -> HomeProgramPhoto:
    """Foto familiar F16 no prefixo patients/<id>/home-programs/.../photos/."""
    professional = Professional(
        email=f"photo-{uuid.uuid4().hex}@x.com", password_hash="x", name="Prescritora"
    )
    db.add(professional)
    await db.flush()
    patient = Patient(
        professional_id=professional.id,
        name="Paciente sintético",
        birth_date=date(2020, 1, 1),
        start_date=date.today(),
        avatar_color="teal",
        diagnosis_keys=[],
    )
    db.add(patient)
    await db.flush()
    program = HomeProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Programa",
        status="active",
        version=2,
        starts_on=date.today(),
        ends_on=date.today() + timedelta(days=7),
    )
    db.add(program)
    await db.flush()
    task = HomeProgramTask(
        program_id=program.id,
        position=0,
        client_task_id=uuid.uuid4(),
        title="Tarefa",
        instructions="Instruções",
        due_on=date.today(),
        goal_id=uuid.uuid4(),
    )
    db.add(task)
    await db.flush()
    check_in = HomeProgramCheckIn(
        program_id=program.id,
        task_id=task.id,
        done=True,
        version=1,
        responded_at=datetime.now(UTC),
    )
    db.add(check_in)
    await db.flush()
    photo = HomeProgramPhoto(
        check_in_id=check_in.id,
        program_id=program.id,
        status=status,
        storage_key=key,
        content_type="image/jpeg",
        size_bytes=10,
        sha256="a" * 64,
    )
    db.add(photo)
    await db.commit()
    await db.refresh(photo)
    return photo


@pytest.mark.asyncio
async def test_janitor_keeps_blob_referenced_by_live_photo(session, monkeypatch):
    """Foto VIGENTE (F16, prefixo patients/.../photos/) nunca é apagada."""
    key = (
        "patients/00000000-0000-0000-0000-000000000009/home-programs/"
        "00000000-0000-0000-0000-000000000010/photos/"
        "00000000-0000-0000-0000-000000000011/photo.jpg"
    )
    photo = await _home_photo(session, key, status="ready")
    task = await _task(session, key)
    assert await storage_key_in_use(session, key) is True
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["kept"] == 1 and summary["deleted"] == 0
    assert calls == []
    await session.refresh(task)
    await session.refresh(photo)
    assert task.status == STORAGE_CLEANUP_KEPT
    assert photo.status == "ready"


@pytest.mark.asyncio
async def test_janitor_removes_blob_of_replaced_photo(session, monkeypatch):
    """Foto substituída/removida (status deleted) libera o blob para o janitor."""
    key = (
        "patients/00000000-0000-0000-0000-000000000009/home-programs/"
        "00000000-0000-0000-0000-000000000010/photos/"
        "00000000-0000-0000-0000-000000000012/photo.jpg"
    )
    photo = await _home_photo(session, key, status="deleted")
    task = await _task(session, key)
    assert await storage_key_in_use(session, key) is False
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["deleted"] == 1 and summary["kept"] == 0
    assert calls == [key]
    await session.refresh(task)
    await session.refresh(photo)
    assert task.status == STORAGE_CLEANUP_DELETED
    assert photo.status == "deleted"  # histórico preservado; só o blob saiu


@pytest.mark.asyncio
async def test_janitor_retries_with_backoff_then_marks_visible_failure(
    session, monkeypatch, caplog
):
    key = "resources/00000000-0000-0000-0000-000000000004/op/flaky.pdf"
    task = await _task(session, key)
    bucket = get_settings().s3_bucket or "bucket-padrao"
    monkeypatch.setattr(
        storage_cleanup_service.storage_service,
        "delete",
        AsyncMock(side_effect=RuntimeError(f"falha em {key} no bucket {bucket}")),
    )
    caplog.set_level("WARNING")

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary["retried"] == 1 and summary["failed"] == 0
    await session.refresh(task)
    assert task.status == STORAGE_CLEANUP_PENDING
    assert task.attempts == 1
    assert task.not_before.replace(tzinfo=UTC) == WAIT + cleanup_backoff(1)
    # diagnóstico sanitizado: sem a chave crua nem o bucket
    assert task.last_error and key not in task.last_error
    assert bucket not in task.last_error

    # Antes do not_before o lote não pega a tarefa de novo.
    summary = await run_storage_cleanup(session, now=WAIT)
    assert summary["selected"] == 0

    # Depois do not_before tenta novamente (backoff crescente).
    current = task.not_before.replace(tzinfo=UTC) + timedelta(seconds=1)
    for expected_attempt in range(2, MAX_CLEANUP_ATTEMPTS + 1):
        summary = await run_storage_cleanup(session, now=current)
        await session.refresh(task)
        if expected_attempt < MAX_CLEANUP_ATTEMPTS:
            assert task.status == STORAGE_CLEANUP_PENDING
            assert task.attempts == expected_attempt
            assert summary["retried"] == 1
            current = task.not_before.replace(tzinfo=UTC) + timedelta(seconds=1)
        else:
            assert task.status == STORAGE_CLEANUP_FAILED
            assert summary["failed"] == 1

    # Falha além do limite fica visível (status failed + ERROR com o id da task),
    # sem descarte silencioso e sem chave crua no log externo.
    assert any(
        record.levelno == logging.ERROR and str(task.id) in record.getMessage()
        for record in caplog.records
    )
    assert all(key not in record.getMessage() for record in caplog.records)
    assert cleanup_backoff(2) > cleanup_backoff(1)
    assert cleanup_backoff(99) == timedelta(hours=24)


@pytest.mark.asyncio
async def test_janitor_respects_not_before_and_terminal_statuses(session, monkeypatch):
    due_key = "resources/00000000-0000-0000-0000-000000000005/op/due.pdf"
    future_key = "resources/00000000-0000-0000-0000-000000000005/op/future.pdf"
    resolved_key = "resources/00000000-0000-0000-0000-000000000005/op/resolved.pdf"
    await _task(session, due_key)
    await _task(session, future_key, not_before=WAIT + timedelta(hours=1))
    await _task(session, resolved_key, status=STORAGE_CLEANUP_RESOLVED)
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    summary = await run_storage_cleanup(session, now=WAIT)

    assert summary == {"selected": 1, "deleted": 1, "kept": 0, "retried": 0, "failed": 0}
    assert calls == [due_key]


@pytest.mark.asyncio
async def test_janitor_batch_is_capped_at_100(session, monkeypatch):
    assert STORAGE_CLEANUP_BATCH_LIMIT == 100
    session.add_all(
        [
            StorageCleanupTask(
                storage_key=f"resources/00000000-0000-0000-0000-000000000006/op/o-{index}.pdf",
                status=STORAGE_CLEANUP_PENDING,
                reason="resource_create",
                not_before=WAIT - timedelta(minutes=1),
            )
            for index in range(STORAGE_CLEANUP_BATCH_LIMIT + 1)
        ]
    )
    await session.commit()
    calls: list[str] = []
    monkeypatch.setattr(
        storage_cleanup_service.storage_service, "delete", _recording_delete(calls)
    )

    first = await run_storage_cleanup(session, limit=10_000, now=WAIT)
    assert first["selected"] == STORAGE_CLEANUP_BATCH_LIMIT
    assert first["deleted"] == STORAGE_CLEANUP_BATCH_LIMIT
    assert len(calls) == STORAGE_CLEANUP_BATCH_LIMIT

    second = await run_storage_cleanup(session, limit=10_000, now=WAIT)
    assert second["deleted"] == 1
    assert len(calls) == STORAGE_CLEANUP_BATCH_LIMIT + 1


def test_redact_storage_key_never_leaks_the_raw_key():
    key = "resources/00000000-0000-0000-0000-000000000007/op/segredo.pdf"
    redacted = redact_storage_key(key)
    assert key not in redacted
    assert "segredo.pdf" not in redacted
    assert redacted == redact_storage_key(key)


def test_sanitize_storage_error_drops_key_and_bucket():
    key = "resources/00000000-0000-0000-0000-000000000008/op/segredo.pdf"
    message = sanitize_storage_error(
        RuntimeError(f"DeleteObject falhou para {key}\ncom bucket korus-one"),
        storage_key=key,
    )
    assert key not in message
    assert "segredo.pdf" not in message
    assert "RuntimeError" in message
    assert "\n" not in message
