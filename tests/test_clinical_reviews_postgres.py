"""PostgreSQL concurrency and rollback invariants for B1/D1.

These tests intentionally use the disposable ``audit_pg_factory`` schema.  The
SQLite suite covers the HTTP shape and ordinary rules; these cases exercise
row locks and fresh identity-map reads on the database used in production.
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.ai import AIReport
from app.models.appointment import Appointment
from app.models.clinical_review import ClinicalReview
from app.models.finance import Receivable, ReceivableItem
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.timeline import TimelineEvent
from app.schemas.clinical_review import (
    ClinicalReviewComplete,
    ClinicalReviewCreate,
    FamilyReportCreate,
)
from app.services import clinical_review_service


async def _seed(factory):
    async with factory() as db:
        professional = Professional(
            email=f"clinical-review-pg-{uuid4().hex}@example.com",
            password_hash="unused",
            name="PG Review",
            specialty="Fonoaudiologia",
            specialty_key="fono",
            email_verified_at=datetime.now(UTC),
        )
        db.add(professional)
        await db.flush()
        patient = Patient(
            professional_id=professional.id,
            name="Paciente Review PG",
            birth_date=date(2020, 1, 1),
            diagnosis_keys=[],
            status="ativo",
            start_date=date.today(),
            avatar_color="teal",
        )
        db.add(patient)
        await db.commit()
        return professional.id, patient.id


async def _create_review(factory, professional_id, patient_id, *, kind="periodic", **kwargs):
    async with factory() as db:
        professional = await db.get(Professional, professional_id)
        review = await clinical_review_service.create_review(
            db,
            patient_id,
            professional,
            ClinicalReviewCreate(kind=kind, **kwargs),
        )
        await db.commit()
        return review.id


@pytest.mark.asyncio
async def test_pg_concurrent_complete_same_command_is_idempotent(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clinical_review_service, "_workflow_flag", enabled)
    professional_id, patient_id = await _seed(audit_pg_factory)
    review_id = await _create_review(audit_pg_factory, professional_id, patient_id)
    async with audit_pg_factory() as db:
        fingerprint = (await db.get(ClinicalReview, review_id)).source_fingerprint

    gate = asyncio.Barrier(2)

    async def complete():
        async with audit_pg_factory() as db:
            actor = await db.get(Professional, professional_id)
            await gate.wait()
            body = ClinicalReviewComplete(
                expected_version=1,
                source_fingerprint=fingerprint,
                idempotency_key="pg-complete-once",
                reviewed=True,
            )
            try:
                row = await clinical_review_service.complete_review(db, patient_id, review_id, actor, body)
                await db.commit()
                return row.status
            except Exception:
                await db.rollback()
                raise

    assert await asyncio.gather(complete(), complete()) == ["completed", "completed"]
    async with audit_pg_factory() as db:
        assert await db.scalar(select(func.count()).select_from(ClinicalReview).where(ClinicalReview.id == review_id, ClinicalReview.status == "completed")) == 1
        row = await db.get(ClinicalReview, review_id)
        assert row.completion_idempotency_key == "pg-complete-once"


@pytest.mark.asyncio
async def test_pg_cancel_waits_for_complete_and_cannot_cancel_completed_review(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clinical_review_service, "_workflow_flag", enabled)
    professional_id, patient_id = await _seed(audit_pg_factory)
    review_id = await _create_review(audit_pg_factory, professional_id, patient_id)
    async with audit_pg_factory() as db:
        fingerprint = (await db.get(ClinicalReview, review_id)).source_fingerprint

    source_locked = asyncio.Event()
    release = asyncio.Event()
    original_capture = clinical_review_service.capture_sources

    async def paused_capture(*args, **kwargs):
        result = await original_capture(*args, **kwargs)
        source_locked.set()
        await release.wait()
        return result

    monkeypatch.setattr(clinical_review_service, "capture_sources", paused_capture)

    async def complete():
        async with audit_pg_factory() as db:
            actor = await db.get(Professional, professional_id)
            row = await clinical_review_service.complete_review(
                db,
                patient_id,
                review_id,
                actor,
                ClinicalReviewComplete(expected_version=1, source_fingerprint=fingerprint, idempotency_key="pg-cancel-race", reviewed=True),
            )
            await db.commit()
            return row.status

    async def cancel():
        async with audit_pg_factory() as db:
            actor = await db.get(Professional, professional_id)
            try:
                row = await clinical_review_service.cancel_review(db, patient_id, review_id, actor, 1)
                await db.commit()
                return row.status
            except Exception:
                await db.rollback()
                raise

    complete_task = asyncio.create_task(complete())
    await asyncio.wait_for(source_locked.wait(), timeout=5)
    cancel_task = asyncio.create_task(cancel())
    await asyncio.sleep(0.1)
    assert not cancel_task.done()
    release.set()
    assert await complete_task == "completed"
    assert await cancel_task == "completed"
    async with audit_pg_factory() as db:
        assert (await db.get(ClinicalReview, review_id)).status == "completed"


@pytest.mark.asyncio
async def test_pg_family_report_creation_is_single_row_under_retry(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clinical_review_service, "_workflow_flag", enabled)
    professional_id, patient_id = await _seed(audit_pg_factory)
    review_id = await _create_review(
        audit_pg_factory,
        professional_id,
        patient_id,
        summary="Orientação revisada",
    )
    async with audit_pg_factory() as db:
        actor = await db.get(Professional, professional_id)
        review = await db.get(ClinicalReview, review_id)
        await clinical_review_service.complete_review(
            db,
            patient_id,
            review_id,
            actor,
            ClinicalReviewComplete(expected_version=1, source_fingerprint=review.source_fingerprint, idempotency_key="pg-report-source", reviewed=True),
        )
        await db.commit()

    event_started = asyncio.Event()
    release = asyncio.Event()
    original_timeline = clinical_review_service.create_timeline_event

    async def paused_timeline(*args, **kwargs):
        event = await original_timeline(*args, **kwargs)
        event_started.set()
        await release.wait()
        return event

    monkeypatch.setattr(clinical_review_service, "create_timeline_event", paused_timeline)

    async def report(key):
        async with audit_pg_factory() as db:
            actor = await db.get(Professional, professional_id)
            try:
                report_row = await clinical_review_service.create_family_report(
                    db, patient_id, review_id, actor, FamilyReportCreate(idempotency_key=key)
                )
                await db.commit()
                return report_row.id
            except Exception:
                await db.rollback()
                raise

    first = asyncio.create_task(report("pg-report-a"))
    await asyncio.wait_for(event_started.wait(), timeout=5)
    second = asyncio.create_task(report("pg-report-b"))
    await asyncio.sleep(0.1)
    assert not second.done()
    release.set()
    report_ids = await asyncio.gather(first, second)
    assert report_ids[0] == report_ids[1]
    async with audit_pg_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AIReport).where(AIReport.patient_id == patient_id, AIReport.type == "pais")) == 1
        assert await db.scalar(select(func.count()).select_from(TimelineEvent).where(TimelineEvent.patient_id == patient_id, TimelineEvent.type == "relatorio")) == 1


@pytest.mark.asyncio
async def test_pg_discharge_finance_veto_rolls_back_status_and_appointment(audit_pg_factory, monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clinical_review_service, "_workflow_flag", enabled)
    professional_id, patient_id = await _seed(audit_pg_factory)
    async with audit_pg_factory() as db:
        appointment = Appointment(
            professional_id=professional_id,
            patient_id=patient_id,
            date=date.today() + timedelta(days=3),
            time=time(10, 0),
            type="Fono",
            duration=50,
            status="pendente",
        )
        db.add(appointment)
        await db.flush()
        appointment_id = appointment.id
        receivable = Receivable(
            professional_id=professional_id,
            patient_id=patient_id,
            patient_name_snapshot="Paciente Review PG",
            payer_name="Responsável",
            payer_document="",
            description="Atendimento",
            issue_date=date.today(),
            competence_date=date.today(),
            due_date=date.today() + timedelta(days=10),
            total_cents=10000,
            status="open",
            origin="appointment",
        )
        db.add(receivable)
        await db.flush()
        receivable_id = receivable.id
        db.add(ReceivableItem(receivable_id=receivable.id, appointment_id=appointment.id, description="Atendimento", quantity=1, unit_cents=10000, total_cents=10000))
        review = await clinical_review_service.create_review(
            db,
            patient_id,
            await db.get(Professional, professional_id),
            ClinicalReviewCreate(kind="discharge", discharge_on=date.today(), final_summary="Alta", family_guidance="Orientações"),
        )
        await db.commit()
        preview = await clinical_review_service.discharge_preview(db, patient_id, review.id, await db.get(Professional, professional_id))
        with pytest.raises(HTTPException) as exc:
            await clinical_review_service.complete_review(
                db,
                patient_id,
                review.id,
                await db.get(Professional, professional_id),
                ClinicalReviewComplete(
                    expected_version=1,
                    source_fingerprint=review.source_fingerprint,
                    idempotency_key="pg-finance-veto",
                    reviewed=True,
                    expected_agenda_fingerprint=preview.agenda_fingerprint,
                    appointment_decisions=[{"appointmentId": appointment.id, "action": "cancel"}],
                ),
            )
        assert exc.value.status_code == 409
        await db.rollback()

    async with audit_pg_factory() as db:
        assert (await db.get(Patient, patient_id)).status == "ativo"
        assert (await db.get(Appointment, appointment_id)).status == "pendente"
        assert (await db.get(Receivable, receivable_id)).status == "open"
