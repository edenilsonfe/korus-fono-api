from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.clinical_review import ClinicalReview
from app.models.evolution import Evolution
from app.models.assessment import Assessment
from app.models.finance import Receivable, ReceivableItem
from app.models.goal import Goal
from app.models.home_program import HomeProgram, HomeProgramCheckIn, HomeProgramTask
from app.models.patient import Patient
from app.models.professional import Professional
from app.core.security import hash_password
from app.services import clinical_review_service
from app.schemas.clinical_review import ClinicalReviewComplete, ClinicalReviewCreate, ClinicalReviewUpdate


@pytest.fixture(autouse=True)
def enable_clinical_review_flag(monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(clinical_review_service, "_workflow_flag", enabled)


@pytest.mark.asyncio
async def test_periodic_review_source_snapshot_and_stale_conflict(db_session, professional, patient):
    evolution = Evolution(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime.now(UTC),
        title="Sessão",
        content="Observação original",
    )
    db_session.add(evolution)
    await db_session.flush()
    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(summary="Revisão inicial", sources=[{"kind": "evolution", "sourceId": evolution.id}]),
    )
    await db_session.commit()
    assert review.source_fingerprint

    evolution.content = "Observação corrigida"
    await db_session.commit()
    with pytest.raises(Exception) as stale:
        await clinical_review_service.complete_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewComplete(
        expected_version=1,
        source_fingerprint=review.source_fingerprint,
        idempotency_key="stale-source-command",
        reviewed=True,
            ),
        )
    assert getattr(stale.value, "status_code", None) == 409

    updated = await clinical_review_service.update_review(
        db_session,
        patient.id,
        review.id,
        professional,
        ClinicalReviewUpdate(expected_version=1, summary="Texto revisado"),
    )
    assert updated.version == 2
    with pytest.raises(Exception) as exc:
        await clinical_review_service.update_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewUpdate(expected_version=1, summary="Texto antigo"),
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_update_rejects_inverted_effective_period(db_session, professional, patient):
    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)),
    )
    await db_session.commit()

    with pytest.raises(Exception) as exc:
        await clinical_review_service.update_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewUpdate(expected_version=1, period_end=date(2025, 12, 31)),
        )

    assert getattr(exc.value, "status_code", None) == 422
    assert review.version == 1
    assert review.period_start == date(2026, 1, 1)
    assert review.period_end == date(2026, 1, 31)


@pytest.mark.asyncio
async def test_family_checkin_snapshot_uses_translated_self_report_without_caregiver_identity(
    db_session, professional, patient
):
    program = HomeProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Programa",
        status="active",
        starts_on=date.today(),
        ends_on=date.today() + timedelta(days=7),
    )
    db_session.add(program)
    await db_session.flush()
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Pedir ajuda",
        area="Comunicação",
        start_date=date.today(),
    )
    db_session.add(goal)
    await db_session.flush()
    task = HomeProgramTask(
        program_id=program.id,
        position=1,
        client_task_id=uuid4(),
        title="Pedir ajuda",
        instructions="Observar a rotina",
        functional_question="Conseguiu pedir ajuda?",
        due_on=date.today(),
        goal_id=goal.id,
    )
    db_session.add(task)
    await db_session.flush()
    checkin = HomeProgramCheckIn(
        program_id=program.id,
        task_id=task.id,
        done=False,
        functional_question=task.functional_question,
        functional_observation="no_opportunity",
        observation_context="home",
    )
    db_session.add(checkin)
    await db_session.flush()

    snapshot = await clinical_review_service._load_source(
        db_session, patient.id, "family_checkin", checkin.id
    )

    assert snapshot["excerpt"] == (
        "Pergunta funcional: Conseguiu pedir ajuda? — "
        "Relato autodeclarado da família: Não houve oportunidade — Contexto: Casa"
    )
    assert "caregiver" not in snapshot["excerpt"].lower()


@pytest.mark.asyncio
async def test_completion_is_idempotent_and_replay_with_other_content_conflicts(db_session, professional, patient):
    review = await clinical_review_service.create_review(
        db_session, patient.id, professional, ClinicalReviewCreate(summary="Concluir")
    )
    first = ClinicalReviewComplete(
        expected_version=1,
        source_fingerprint=review.source_fingerprint,
        idempotency_key="review-command-1",
        reviewed=True,
    )
    completed = await clinical_review_service.complete_review(db_session, patient.id, review.id, professional, first)
    await db_session.commit()
    again = await clinical_review_service.complete_review(db_session, patient.id, review.id, professional, first)
    assert again.id == completed.id

    with pytest.raises(Exception) as exc:
        await clinical_review_service.complete_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewComplete(expected_version=1, source_fingerprint=review.source_fingerprint, idempotency_key="review-command-2", reviewed=True),
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_discharge_requires_explicit_future_appointment_decisions(db_session, professional, patient):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=3),
        time=time(10, 0),
        type="Fono",
        duration=50,
        status="pendente",
    )
    db_session.add(appointment)
    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="discharge", discharge_on=date.today(), final_summary="Alta", family_guidance="Orientações"),
    )
    await db_session.commit()
    appointment_id = appointment.id
    preview = await clinical_review_service.discharge_preview(db_session, patient.id, review.id, professional)
    with pytest.raises(Exception) as exc:
        await clinical_review_service.complete_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewComplete(
                expected_version=1,
                source_fingerprint=review.source_fingerprint,
                idempotency_key="discharge-command-1",
                reviewed=True,
                expected_agenda_fingerprint=preview.agenda_fingerprint,
            ),
        )
    assert getattr(exc.value, "status_code", None) == 409
    await db_session.rollback()
    assert (await db_session.scalar(select(Appointment.status).where(Appointment.id == appointment_id))) == "pendente"


@pytest.mark.asyncio
async def test_discharge_closes_due_cycle_without_mutating_previous_review(db_session, professional, patient):
    periodic = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="periodic", next_review_on=date.today() - timedelta(days=1)),
    )
    await clinical_review_service.complete_review(
        db_session,
        patient.id,
        periodic.id,
        professional,
        ClinicalReviewComplete(expected_version=1, source_fingerprint=periodic.source_fingerprint, idempotency_key="periodic-due", reviewed=True),
    )
    discharge = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="discharge", discharge_on=date.today(), final_summary="Alta", family_guidance="Orientações"),
    )
    await clinical_review_service.complete_review(
        db_session,
        patient.id,
        discharge.id,
        professional,
        ClinicalReviewComplete(expected_version=1, source_fingerprint=discharge.source_fingerprint, idempotency_key="discharge-due", reviewed=True, expected_agenda_fingerprint=clinical_review_service._hash([])),
    )
    assert periodic.next_review_on == date.today() - timedelta(days=1)
    assert await clinical_review_service.list_due(db_session, professional) == []


@pytest.mark.asyncio
async def test_due_uses_only_latest_completed_periodic_review(db_session, professional, patient):
    first = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="periodic", next_review_on=date.today() - timedelta(days=2)),
    )
    await clinical_review_service.complete_review(
        db_session,
        patient.id,
        first.id,
        professional,
        ClinicalReviewComplete(expected_version=1, source_fingerprint=first.source_fingerprint, idempotency_key="due-first", reviewed=True),
    )
    second = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="periodic", next_review_on=date.today() + timedelta(days=14)),
    )
    await clinical_review_service.complete_review(
        db_session,
        patient.id,
        second.id,
        professional,
        ClinicalReviewComplete(expected_version=1, source_fingerprint=second.source_fingerprint, idempotency_key="due-second", reviewed=True),
    )
    await db_session.commit()

    assert await clinical_review_service.list_due(db_session, professional) == []
    second.next_review_on = date.today() - timedelta(days=1)
    await db_session.commit()
    due = await clinical_review_service.list_due(db_session, professional)
    assert [item.id for item in due] == [second.id]


@pytest.mark.asyncio
async def test_discharge_cannot_cancel_financially_linked_future_appointment(db_session, professional, patient):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=4),
        time=time(11, 0),
        type="Fono",
        duration=50,
        status="pendente",
    )
    db_session.add(appointment)
    await db_session.flush()
    receivable = Receivable(
        professional_id=professional.id,
        patient_id=patient.id,
        patient_name_snapshot=patient.name,
        payer_name="Maria Silva",
        payer_document="",
        description="Atendimento",
        issue_date=date.today(),
        competence_date=date.today(),
        due_date=date.today() + timedelta(days=10),
        total_cents=10000,
        status="open",
        origin="appointment",
    )
    db_session.add(receivable)
    await db_session.flush()
    db_session.add(ReceivableItem(
        receivable_id=receivable.id,
        appointment_id=appointment.id,
        description="Atendimento",
        quantity=1,
        unit_cents=10000,
        total_cents=10000,
    ))
    appointment_id = appointment.id
    receivable_id = receivable.id
    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="discharge", discharge_on=date.today(), final_summary="Alta", family_guidance="Orientações"),
    )
    await db_session.commit()
    preview = await clinical_review_service.discharge_preview(db_session, patient.id, review.id, professional)
    assert preview.appointments[0].has_financial_link is True
    with pytest.raises(Exception) as exc:
        await clinical_review_service.complete_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewComplete(
                expected_version=1,
                source_fingerprint=review.source_fingerprint,
                idempotency_key="discharge-finance",
                reviewed=True,
                expected_agenda_fingerprint=preview.agenda_fingerprint,
                appointment_decisions=[{"appointmentId": appointment_id, "action": "cancel"}],
            ),
        )
    assert getattr(exc.value, "status_code", None) == 409
    await db_session.rollback()
    assert (await db_session.scalar(select(Appointment.status).where(Appointment.id == appointment_id))) == "pendente"
    assert (await db_session.scalar(select(Receivable.status).where(Receivable.id == receivable_id))) == "open"


@pytest.mark.asyncio
async def test_review_acl_rejects_other_professional(db_session, patient):
    other = Professional(
        email="other-review@example.com",
        password_hash=hash_password("testpass123"),
        name="Outra profissional",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(Exception) as exc:
        await clinical_review_service.create_review(
            db_session, patient.id, other, ClinicalReviewCreate(summary="Fora do escopo")
        )
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_discharge_home_program_ids_are_scoped_to_patient(db_session, professional, patient):
    other_patient = Patient(
        professional_id=professional.id,
        name="Outro paciente",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date.today(),
        avatar_color="teal",
    )
    db_session.add(other_patient)
    await db_session.flush()
    foreign_program = HomeProgram(
        patient_id=other_patient.id,
        created_by_professional_id=professional.id,
        title="Programa alheio",
        status="active",
        starts_on=date.today(),
        ends_on=date.today() + timedelta(days=7),
    )
    db_session.add(foreign_program)
    await db_session.flush()
    with pytest.raises(Exception) as exc:
        await clinical_review_service.create_review(
            db_session,
            patient.id,
            professional,
            ClinicalReviewCreate(
                kind="discharge",
                discharge_on=date.today(),
                home_program_ids=[foreign_program.id],
            ),
        )
    assert getattr(exc.value, "status_code", None) == 409

    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(kind="discharge", discharge_on=date.today()),
    )
    review.home_program_ids = [str(foreign_program.id)]
    await db_session.commit()
    with pytest.raises(Exception) as preview_exc:
        await clinical_review_service.discharge_preview(db_session, patient.id, review.id, professional)
    assert getattr(preview_exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_comparison_source_is_snapshot_and_detects_target_change(db_session, professional, patient):
    base = Assessment(
        patient_id=patient.id, professional_id=professional.id, protocol_id="abfw", date=date.today() - timedelta(days=20),
        result="Inicial", percentage=20, interpretation="", fields=[], answers={"item": "a"}, scores={"total": 20}, status="completed",
    )
    target = Assessment(
        patient_id=patient.id, professional_id=professional.id, protocol_id="abfw", date=date.today() - timedelta(days=2),
        result="Atual", percentage=40, interpretation="", fields=[], answers={"item": "b"}, scores={"total": 40}, status="completed",
    )
    db_session.add_all([base, target])
    await db_session.flush()
    review = await clinical_review_service.create_review(
        db_session,
        patient.id,
        professional,
        ClinicalReviewCreate(sources=[{"kind": "comparison", "sourceId": base.id, "comparisonTargetId": target.id}]),
    )
    await db_session.commit()
    target.percentage = 50
    await db_session.commit()
    with pytest.raises(Exception) as exc:
        await clinical_review_service.complete_review(
            db_session,
            patient.id,
            review.id,
            professional,
            ClinicalReviewComplete(expected_version=1, source_fingerprint=review.source_fingerprint, idempotency_key="comparison-stale", reviewed=True),
        )
    assert getattr(exc.value, "status_code", None) == 409
