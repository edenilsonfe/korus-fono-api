from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import worker
from app.db.base import Base
from app.models.attachment import Attachment
from app.models.home_program import HomeProgramPhoto
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.storage_cleanup import StorageCleanupTask
from app.services.storage_cleanup_service import STORAGE_CLEANUP_BATCH_LIMIT


@pytest.mark.asyncio
async def test_worker_validates_runtime_settings_on_startup(monkeypatch):
    settings = object()
    validate_settings = Mock()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "validate_settings", validate_settings, raising=False)

    await worker.WorkerSettings.on_startup({})

    validate_settings.assert_called_once_with(settings)


def _cron_job(name: str):
    for job in worker.WorkerSettings.cron_jobs:
        if getattr(job.coroutine, "__name__", "") == name:
            return job
    raise AssertionError(f"cron {name} não registrado em WorkerSettings")


def test_worker_registers_storage_cleanup_cron_every_15_minutes():
    job = _cron_job("run_storage_cleanup_job")
    assert job.minute == {2, 17, 32, 47}
    assert job.run_at_startup is False
    assert STORAGE_CLEANUP_BATCH_LIMIT == 100

    # Os crons existentes continuam registrados, sem reordenação.
    names = [
        getattr(existing.coroutine, "__name__", None)
        for existing in worker.WorkerSettings.cron_jobs
    ]
    assert names == [
        "run_whatsapp_scheduler",
        "retry_google_calendar_syncs",
        "run_affiliate_maintenance",
        "run_storage_cleanup_job",
    ]


@pytest.mark.asyncio
async def test_run_storage_cleanup_job_runs_janitor_with_capped_batch(monkeypatch):
    captured: dict = {}

    class _Session:
        async def __aenter__(self):
            return "session"

        async def __aexit__(self, *args):
            return False

    async def fake_run_cleanup(db, *, limit):
        captured["db"] = db
        captured["limit"] = limit
        return {"selected": 0}

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(
        "app.services.storage_cleanup_service.run_storage_cleanup", fake_run_cleanup
    )

    await worker.run_storage_cleanup_job({})

    assert captured == {"db": "session", "limit": STORAGE_CLEANUP_BATCH_LIMIT}


@pytest.mark.asyncio
async def test_run_storage_cleanup_job_deletes_orphan_end_to_end(monkeypatch):
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
                    HomeProgramPhoto.__table__,
                ],
            )
        )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    orphan_key = "resources/00000000-0000-0000-0000-000000000009/op/orfao.pdf"
    async with maker() as session:
        session.add(
            StorageCleanupTask(
                storage_key=orphan_key,
                status="pending",
                reason="resource_create",
            )
        )
        await session.commit()

    deleted: list[str] = []

    async def fake_delete(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(worker, "AsyncSessionLocal", maker)
    monkeypatch.setattr(
        "app.services.storage_cleanup_service.storage_service.delete", fake_delete
    )

    await worker.run_storage_cleanup_job({})

    async with maker() as session:
        row = (await session.execute(select(StorageCleanupTask))).scalar_one()
        assert row.status == "deleted"
    assert deleted == [orphan_key]
    await engine.dispose()
