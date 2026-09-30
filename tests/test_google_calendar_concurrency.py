"""Claims, requeue and OAuth consumption on disposable PostgreSQL."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarSyncRecord
from app.models.patient import Patient
from app.models.professional import Professional
from app.services import google_calendar_service as service


async def _seed(factory):
    async with factory() as db:
        professional = Professional(email=f"google-pg-{uuid4().hex}@example.com", password_hash="unused",
                                    name="Teste PG", email_verified_at=datetime.now(UTC))
        db.add(professional)
        await db.flush()
        patient = Patient(professional_id=professional.id, name="Paciente PG", birth_date=date(2020, 1, 1),
                          diagnosis_keys=["tea"], status="ativo", start_date=date.today(), avatar_color="teal")
        db.add(patient)
        await db.flush()
        appointment = Appointment(professional_id=professional.id, patient_id=patient.id,
                                  date=date.today() + timedelta(days=1), time=time(9), duration=50,
                                  type="Terapia", status="pendente")
        connection = GoogleCalendarConnection(professional_id=professional.id,
                                              encrypted_refresh_token="mocked", connected_at=datetime.now(UTC))
        db.add_all([appointment, connection])
        await db.flush()
        record = await service.queue_appointment_sync(db, appointment, patient.name)
        await db.commit()
        return professional, patient.id, appointment.id, record.id


@pytest.mark.parametrize("change", ["edit", "cancel"])
async def test_concurrent_dispatch_and_requeue_preserve_newest_version(audit_pg_factory, monkeypatch, change):
    factory = audit_pg_factory
    monkeypatch.setattr(service, "AsyncSessionLocal", factory)
    monkeypatch.setattr(service, "_access_token", AsyncMock(return_value="mocked"))
    monkeypatch.setattr(service, "_find_existing_event", AsyncMock(return_value=None))
    _professional, _patient_id, appointment_id, record_id = await _seed(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    events, creates = {}, []

    async def google_request(_token, method, path, *, json_body=None):
        event_id = path.rsplit("/", 1)[-1]
        if method == "POST":
            event_id = json_body["id"]
            creates.append(event_id)
            events[event_id] = json_body
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        elif method == "DELETE":
            events.pop(event_id, None)
            return httpx.Response(204)
        else:
            assert method == "PUT"
            events[event_id] = json_body
        return httpx.Response(200, json={"id": event_id})

    monkeypatch.setattr(service, "_google_request", google_request)
    first = asyncio.create_task(service.dispatch_sync_record(record_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(service.dispatch_sync_record(record_id), 2)
        async with factory() as db:
            appointment = await db.get(Appointment, appointment_id)
            if change == "cancel":
                appointment.status = "cancelado"
            else:
                appointment.time = time(11)
            record = await service.queue_appointment_sync(db, appointment, "Paciente PG")
            assert record.status == "processing" and record.sync_version == 2
            await db.commit()
        await asyncio.wait_for(service.dispatch_sync_record(record_id), 2)
    finally:
        release.set()
        await asyncio.wait_for(first, 5)
    assert len(creates) == 1
    if change == "cancel":
        assert events == {}
    else:
        assert len(events) == 1
        assert "T11:00:00" in next(iter(events.values()))["start"]["dateTime"]
    async with factory() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        assert record.status == "synced" and record.sync_version == 2
        assert record.processing_token is None and record.last_error is None


async def test_active_claim_is_not_retried_but_expired_claim_is_recovered(audit_pg_factory, monkeypatch):
    factory = audit_pg_factory
    monkeypatch.setattr(service, "AsyncSessionLocal", factory)
    access = AsyncMock(return_value="mocked")
    monkeypatch.setattr(service, "_access_token", access)
    monkeypatch.setattr(service, "_find_existing_event", AsyncMock(return_value=None))
    request = AsyncMock(return_value=httpx.Response(200, json={"id": "remote-id"}))
    monkeypatch.setattr(service, "_google_request", request)
    _professional, _patient_id, _appointment_id, record_id = await _seed(factory)
    async with factory() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        record.status, record.processing_token = "processing", uuid4()
        record.processing_started_at = datetime.now(UTC)
        await db.commit()
    await service.retry_pending_syncs()
    access.assert_not_awaited()
    async with factory() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        record.processing_started_at = datetime.now(UTC) - timedelta(minutes=11)
        await db.commit()
    await service.retry_pending_syncs()
    access.assert_awaited_once()
    async with factory() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        assert record.status == "synced" and record.processing_token is None


async def test_oauth_transaction_has_one_concurrent_consumer(audit_pg_factory, monkeypatch):
    factory = audit_pg_factory
    professional, *_ = await _seed(factory)
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "google_calendar_client_id", "mocked")
    monkeypatch.setattr(get_settings(), "google_calendar_client_secret", "mocked")
    monkeypatch.setattr(get_settings(), "google_calendar_credential_encryption_key", "mocked")
    from urllib.parse import parse_qs, urlparse
    async with factory() as db:
        url = await service.begin_authorization(db, professional, "initiating-browser")
    state = parse_qs(urlparse(url).query)["state"][0]

    async def consume():
        async with factory() as db:
            return await service.consume_authorization(db, state, "initiating-browser")

    results = await asyncio.gather(consume(), consume(), return_exceptions=True)
    assert results.count((professional.id, professional.token_version)) == 1
    assert sum(isinstance(result, service.GoogleCalendarError) for result in results) == 1


async def test_first_concurrent_enqueue_creates_one_record(audit_pg_factory):
    from sqlalchemy import delete, func
    factory = audit_pg_factory
    _professional, _patient_id, appointment_id, record_id = await _seed(factory)
    async with factory() as db:
        await db.execute(delete(GoogleCalendarSyncRecord).where(GoogleCalendarSyncRecord.id == record_id))
        await db.commit()

    async def queue():
        async with factory() as db:
            appointment = await db.get(Appointment, appointment_id)
            record = await service.queue_appointment_sync(db, appointment, "Paciente PG")
            await db.commit()
            return record.id

    ids = await asyncio.gather(queue(), queue())
    assert ids[0] == ids[1]
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GoogleCalendarSyncRecord)) == 1
        assert (await db.get(GoogleCalendarSyncRecord, ids[0])).sync_version == 2


async def test_expired_worker_cannot_finish_another_workers_claim(audit_pg_factory, monkeypatch):
    factory = audit_pg_factory
    monkeypatch.setattr(service, "AsyncSessionLocal", factory)
    monkeypatch.setattr(service, "_access_token", AsyncMock(return_value="mocked"))
    monkeypatch.setattr(service, "_find_existing_event", AsyncMock(return_value=None))
    *_, record_id = await _seed(factory)
    entered, recovered = asyncio.Event(), asyncio.Event()
    release_old, release_new = asyncio.Event(), asyncio.Event()

    async def request(_token, method, path, *, json_body=None):
        if method == "POST":
            entered.set()
            await asyncio.wait_for(release_old.wait(), 5)
            event_id = json_body["id"]
        else:
            assert method == "PUT"
            recovered.set()
            await asyncio.wait_for(release_new.wait(), 5)
            event_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": event_id})

    monkeypatch.setattr(service, "_google_request", request)
    old = asyncio.create_task(service.dispatch_sync_record(record_id))
    new = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as db:
            record = await db.get(GoogleCalendarSyncRecord, record_id)
            old_claim = record.processing_token
            record.processing_started_at = datetime.now(UTC) - timedelta(minutes=11)
            await db.commit()
        new = asyncio.create_task(service.dispatch_sync_record(record_id))
        await asyncio.wait_for(recovered.wait(), 5)
        release_old.set()
        await asyncio.wait_for(old, 5)
        async with factory() as db:
            record = await db.get(GoogleCalendarSyncRecord, record_id)
            assert record.status == "processing"
            assert record.processing_token is not None and record.processing_token != old_claim
    finally:
        release_old.set()
        release_new.set()
        await asyncio.wait_for(old, 5)
        if new:
            await asyncio.wait_for(new, 5)
    async with factory() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        assert record.status == "synced" and record.processing_token is None


async def test_google_migration_preserves_existing_sync_and_credentials(audit_pg_factory):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    factory = audit_pg_factory
    professional, *_, record_id = await _seed(factory)
    path = Path(__file__).resolve().parent.parent / "alembic/versions/gc20260930a_google_calendar_oauth_and_sync_claims.py"
    spec = importlib.util.spec_from_file_location("google_audit_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            connection.execute(text("UPDATE google_calendar_sync_records SET google_event_id='legacy-id', status='processing', attempt_count=3"))
            migration.upgrade()

    async with factory() as db:
        await (await db.connection()).run_sync(migrate)
        await db.commit()
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        assert (record.google_event_id, record.status, record.attempt_count, record.sync_version) == (
            "legacy-id", "processing", 3, 1,
        )
        assert record.processing_token is None and record.processing_started_at is None
        connection = await db.scalar(select(GoogleCalendarConnection).where(
            GoogleCalendarConnection.professional_id == professional.id,
        ))
        assert connection.encrypted_refresh_token == "mocked"
