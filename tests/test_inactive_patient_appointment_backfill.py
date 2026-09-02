from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.patient_appointment_service import (
    backfill_inactive_patient_appointments,
    get_inactive_patient_appointment_inventory,
)
from scripts.backfill_inactive_patient_appointments import main


CLINIC_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _appointment(*, professional_id, patient_id, day, status):
    return Appointment(
        professional_id=professional_id,
        patient_id=patient_id,
        date=day,
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status=status,
    )


@pytest.mark.asyncio
async def test_inactive_patient_backfill_is_guarded_silent_and_idempotent(
    db_session,
    professional,
    patient,
    monkeypatch,
):
    other_professional = Professional(
        email="backfill-other@example.com",
        password_hash="not-used",
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other_professional)
    await db_session.flush()
    other_inactive_patient = Patient(
        professional_id=other_professional.id,
        name="Paciente inativo 2",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="inativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    patient.status = "inativo"
    db_session.add(other_inactive_patient)
    await db_session.flush()

    eligible_pending = _appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        day=date(2026, 9, 3),
        status="pendente",
    )
    eligible_confirmed = _appointment(
        professional_id=other_professional.id,
        patient_id=other_inactive_patient.id,
        day=date(2026, 9, 4),
        status="confirmado",
    )
    past_pending = _appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        day=date(2026, 9, 1),
        status="pendente",
    )
    terminal_future = _appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        day=date(2026, 9, 5),
        status="concluido",
    )
    active_patient = Patient(
        professional_id=professional.id,
        name="Paciente ativo",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(active_patient)
    await db_session.flush()
    active_patient_future = _appointment(
        professional_id=professional.id,
        patient_id=active_patient.id,
        day=date(2026, 9, 6),
        status="confirmado",
    )
    db_session.add_all(
        [
            eligible_pending,
            eligible_confirmed,
            past_pending,
            terminal_future,
            active_patient_future,
            NotificationSettings(
                professional_id=professional.id,
                whatsapp_enabled=True,
                whatsapp_events={"appointment_cancelled": True},
            ),
            NotificationSettings(
                professional_id=other_professional.id,
                whatsapp_enabled=True,
                whatsapp_events={"appointment_cancelled": True},
            ),
        ]
    )
    await db_session.commit()

    inventory = await get_inactive_patient_appointment_inventory(
        db_session,
        clinic_now=CLINIC_NOW,
    )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLINIC_NOW

    monkeypatch.setattr(
        "app.services.patient_appointment_service.datetime",
        FixedDatetime,
    )
    monkeypatch.setattr(
        "app.services.patient_appointment_service.ZoneInfo",
        lambda _key: timezone.utc,
    )
    assert await get_inactive_patient_appointment_inventory(db_session) == inventory

    assert inventory.patient_count == 2
    assert inventory.professional_count == 2
    assert inventory.appointment_count == 2
    assert inventory.pending_count == 1
    assert inventory.confirmed_count == 1
    assert len(inventory.manifest_sha256) == 64

    with pytest.raises(RuntimeError, match="contagem de pacientes mudou"):
        await backfill_inactive_patient_appointments(
            db_session,
            expected_patient_count=3,
            expected_appointment_count=inventory.appointment_count,
            expected_manifest_sha256=inventory.manifest_sha256,
            clinic_now=CLINIC_NOW,
        )

    with pytest.raises(RuntimeError, match="contagem de agendamentos mudou"):
        await backfill_inactive_patient_appointments(
            db_session,
            expected_patient_count=2,
            expected_appointment_count=3,
            expected_manifest_sha256=inventory.manifest_sha256,
            clinic_now=CLINIC_NOW,
        )

    with pytest.raises(RuntimeError, match="conjunto de agendamentos mudou"):
        await backfill_inactive_patient_appointments(
            db_session,
            expected_patient_count=inventory.patient_count,
            expected_appointment_count=inventory.appointment_count,
            expected_manifest_sha256="0" * 64,
            clinic_now=CLINIC_NOW,
        )

    result, event_logs, google_records = await backfill_inactive_patient_appointments(
        db_session,
        expected_patient_count=inventory.patient_count,
        expected_appointment_count=inventory.appointment_count,
        expected_manifest_sha256=inventory.manifest_sha256,
        clinic_now=CLINIC_NOW,
    )
    await db_session.commit()

    assert result == inventory
    assert event_logs == []
    assert google_records == []
    statuses = dict(
        (
            await db_session.execute(
                select(Appointment.id, Appointment.status).where(
                    Appointment.id.in_(
                        [
                            eligible_pending.id,
                            eligible_confirmed.id,
                            past_pending.id,
                            terminal_future.id,
                            active_patient_future.id,
                        ]
                    )
                )
            )
        ).all()
    )
    assert statuses[eligible_pending.id] == "cancelado"
    assert statuses[eligible_confirmed.id] == "cancelado"
    assert statuses[past_pending.id] == "pendente"
    assert statuses[terminal_future.id] == "concluido"
    assert statuses[active_patient_future.id] == "confirmado"

    repeated = await get_inactive_patient_appointment_inventory(
        db_session,
        clinic_now=CLINIC_NOW,
    )
    assert repeated.appointment_count == 0


def test_inactive_patient_backfill_cli_requires_all_apply_guards(capsys):
    assert main(["--apply"]) == 2
    error = capsys.readouterr().err
    assert "--expected-patient-count" in error
    assert "--expected-appointment-count" in error
    assert "--expected-sha256" in error
