from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.patient import Patient
from app.schemas.patient import PatientCreate, PatientUpdate


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


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

