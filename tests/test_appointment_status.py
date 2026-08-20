from datetime import date, time

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appointment import Appointment
from app.schemas.appointment import AppointmentUpdate


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


def test_appointment_status_contract_accepts_falta_and_rejects_unknown_values():
    assert AppointmentUpdate(status="falta").status == "falta"

    with pytest.raises(ValidationError):
        AppointmentUpdate(status="desconhecido")


@pytest.mark.asyncio
async def test_professional_can_mark_appointment_as_falta(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=time(9, 0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    response = await api_client.patch(
        f"/api/v1/appointments/{appointment.id}",
        headers=auth_headers,
        json={"status": "falta"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "falta"
    await db_session.refresh(appointment)
    assert appointment.status == "falta"


@pytest.mark.asyncio
async def test_appointment_marked_as_falta_cannot_be_completed(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=time(9, 0),
        type="Terapia individual",
        duration=50,
        status="falta",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    response = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={"billingMode": "courtesy"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Um atendimento com falta não pode ser concluído"
