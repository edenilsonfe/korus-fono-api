"""Separate PostgreSQL transactions exercise the security/integrity locks."""
import asyncio
from datetime import date, time

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.ai import AIReport, AIReportRevision
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.refresh_session import RefreshSession
from app.schemas.ai import AIReportUpdate
from app.schemas.schedule_block import ScheduleBlockCreate
from app.services.refresh_token_service import create_refresh_session, revoke_refresh_session, rotate_refresh_session, RefreshSessionInvalidated
from app.services.report_service import revise_report
from app.services.schedule_block_service import ensure_appointment_slot_available, create_schedule_block


async def seed(factory):
    async with factory() as db:
        owner = Professional(email="race@example.com", name="Race", password_hash="unused")
        db.add(owner)
        await db.flush()
        patient = Patient(professional_id=owner.id, name="Synthetic", birth_date=date(2020, 1, 1), start_date=date.today(), avatar_color="teal", diagnosis_keys=[])
        db.add(patient)
        await db.commit()
        return owner.id, patient.id


@pytest.mark.parametrize("second_is_block", [False, True])
async def test_empty_slot_has_only_one_winner(audit_pg_factory, second_is_block):
    factory = audit_pg_factory
    owner, patient = await seed(factory)
    locked, waiting = asyncio.Event(), asyncio.Event()
    day = date(2030, 1, 1)

    async def reserve():
        async with factory() as db:
            await ensure_appointment_slot_available(db, owner, day, time(10), 50)
            locked.set()
            await waiting.wait()
            await asyncio.sleep(0.05)
            db.add(Appointment(professional_id=owner, patient_id=patient, date=day, time=time(10), duration=50, type="Terapia"))
            await db.commit()

    async def competing():
        await locked.wait()
        async with factory() as db:
            waiting.set()
            with pytest.raises(HTTPException) as exc:
                if second_is_block:
                    await create_schedule_block(db, owner, ScheduleBlockCreate(start_date=day, end_date=day, all_day=True, reason="Teste"))
                else:
                    await ensure_appointment_slot_available(db, owner, day, time(10), 50)
            assert exc.value.status_code == 409

    await asyncio.wait_for(asyncio.gather(reserve(), competing()), timeout=5)


async def test_refresh_race_returns_the_same_rotated_token(audit_pg_factory):
    factory = audit_pg_factory
    owner, _ = await seed(factory)
    async with factory() as db:
        raw = await create_refresh_session(db, await db.get(Professional, owner))
        await db.commit()

    async def rotate():
        async with factory() as db:
            try:
                _, outcome = await rotate_refresh_session(db, raw)
            except RefreshSessionInvalidated:
                outcome = "revoked"
            await db.commit()
            return outcome

    outcomes = await asyncio.wait_for(asyncio.gather(rotate(), rotate()), timeout=5)
    assert outcomes[0] == outcomes[1] and outcomes[0] != "revoked"
    async with factory() as reader:
        assert (await reader.get(Professional, owner)).token_version == 0
        sessions = (await reader.scalars(select(RefreshSession))).all()
        assert len(sessions) == 2 and sum(session.revoked_at is None for session in sessions) == 1


async def test_logout_closes_an_overlapping_refresh(audit_pg_factory):
    factory = audit_pg_factory
    owner, _ = await seed(factory)
    async with factory() as db:
        raw = await create_refresh_session(db, await db.get(Professional, owner))
        await db.commit()
    rotated, logging_out = asyncio.Event(), asyncio.Event()

    async def refresh():
        async with factory() as db:
            await rotate_refresh_session(db, raw)
            rotated.set()
            await logging_out.wait()
            await asyncio.sleep(0.05)
            await db.commit()

    async def logout():
        await rotated.wait()
        async with factory() as db:
            logging_out.set()
            await revoke_refresh_session(db, raw)
            await db.commit()

    await asyncio.wait_for(asyncio.gather(refresh(), logout()), timeout=5)
    async with factory() as reader:
        sessions = (await reader.scalars(select(RefreshSession))).all()
        assert len(sessions) == 2 and all(session.revoked_at for session in sessions)


async def test_concurrent_report_revisions_preserve_every_version(audit_pg_factory):
    factory = audit_pg_factory
    owner, patient = await seed(factory)
    async with factory() as db:
        report = AIReport(professional_id=owner, patient_id=patient, type="clinico", date=date.today(), preview="Original", content="Original", status="finalized")
        db.add(report)
        await db.commit()
        report_id = report.id

    async def revise(content):
        async with factory() as db:
            await revise_report(db, report_id, owner, AIReportUpdate(content=content))
            await db.commit()

    await asyncio.wait_for(asyncio.gather(revise("A"), revise("B")), timeout=5)
    async with factory() as reader:
        current = await reader.get(AIReport, report_id)
        revisions = (await reader.scalars(select(AIReportRevision))).all()
        assert len(revisions) == 2
        assert {current.content, *(row.content for row in revisions)} == {"Original", "A", "B"}
