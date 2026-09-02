from datetime import date, time, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.schemas.patient import PatientCreate, PatientUpdate


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)
    monkeypatch.setattr(
        "app.services.patient_appointment_service.ZoneInfo",
        lambda _key: timezone(timedelta(hours=-3)),
    )


def test_patient_status_contract_accepts_inativo_and_rejects_unknown_values():
    assert (
        PatientCreate(
            name="Paciente inativo",
            birthDate="2020-01-01",
            diagnosisKeys=["tea"],
            status="inativo",
        ).status
        == "inativo"
    )
    assert PatientUpdate(status="inativo").status == "inativo"

    with pytest.raises(ValidationError):
        PatientUpdate(status="desconhecido")


@pytest.mark.asyncio
async def test_list_patients_filters_inactive_status(
    api_client, db_session, professional, patient, auth_headers
):
    inactive_patient = Patient(
        professional_id=professional.id,
        name="Ana Inativa",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=["tea"],
        status="inativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(inactive_patient)
    await db_session.commit()

    response = await api_client.get(
        "/api/v1/patients?status=inativo",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == [str(inactive_patient.id)]
    assert response.json()["items"][0]["status"] == "inativo"


@pytest.mark.asyncio
async def test_marking_patient_inactive_cancels_only_cancellable_future_appointments(
    api_client, db_session, professional, patient, auth_headers, monkeypatch
):
    past_pending = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() - timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="pendente",
    )
    future_pending = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="pendente",
    )
    future_confirmed = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=2),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="confirmado",
    )
    future_terminal = [
        Appointment(
            professional_id=professional.id,
            patient_id=patient.id,
            date=date.today() + timedelta(days=3 + index),
            time=time(10, 0),
            type="Terapia",
            duration=50,
            status=appointment_status,
        )
        for index, appointment_status in enumerate(("cancelado", "concluido", "falta"))
    ]
    db_session.add_all([
        past_pending,
        future_pending,
        future_confirmed,
        *future_terminal,
        NotificationSettings(
            professional_id=professional.id,
            whatsapp_enabled=True,
            whatsapp_events={"appointment_cancelled": True},
        ),
    ])
    await db_session.commit()
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.patients.enqueue_whatsapp_appointment_event_log",
        enqueue,
    )

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}",
        headers=auth_headers,
        json={"status": "inativo"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "inativo"
    await db_session.refresh(past_pending)
    await db_session.refresh(future_pending)
    await db_session.refresh(future_confirmed)
    for appointment in future_terminal:
        await db_session.refresh(appointment)
    assert past_pending.status == "pendente"
    assert future_pending.status == "cancelado"
    assert future_confirmed.status == "cancelado"
    assert [appointment.status for appointment in future_terminal] == [
        "cancelado",
        "concluido",
        "falta",
    ]
    event_log_count = await db_session.scalar(
        select(func.count())
        .select_from(NotificationMessageLog)
        .where(
            NotificationMessageLog.patient_id == patient.id,
            NotificationMessageLog.notification_type == "appointment_cancelled",
        )
    )
    assert event_log_count == 0
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_patient_update_without_inactive_transition_preserves_future_appointments(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}",
        headers=auth_headers,
        json={"status": "pausado"},
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(appointment)
    assert appointment.status == "confirmado"
