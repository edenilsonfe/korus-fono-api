from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.patient import Patient


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.parametrize("address", [None, "", "  ", "  Rua das Flores, 123, São Paulo/SP  "])
async def test_create_patient_persists_optional_address(
    api_client, db_session, auth_headers, address
):
    body = {"name": "Paciente teste", "birthDate": "2020-01-01", "diagnosisKeys": ["tea"]}
    if address is not None:
        body["address"] = address
    response = await api_client.post("/api/v1/patients", headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    expected = address.strip() or None if address is not None else None
    data = response.json()
    assert data["address"] == expected
    await db_session.commit()
    saved = await db_session.get(Patient, UUID(data["id"]))
    await db_session.refresh(saved)
    assert saved.address == expected

    detail = await api_client.get(f"/api/v1/patients/{data['id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["address"] == expected
    listing = await api_client.get("/api/v1/patients", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    assert next(item for item in listing.json()["items"] if item["id"] == data["id"])["address"] == expected


@pytest.mark.parametrize("clear_value", [None, "", "   "])
async def test_update_patient_changes_preserves_and_clears_address(
    api_client, db_session, patient, auth_headers, clear_value
):
    url = f"/api/v1/patients/{patient.id}"
    response = await api_client.patch(url, headers=auth_headers, json={"address": "  Rua Nova, 42  "})
    assert response.status_code == 200, response.text
    assert response.json()["address"] == "Rua Nova, 42"

    response = await api_client.patch(url, headers=auth_headers, json={"name": "Nome atualizado"})
    assert response.status_code == 200, response.text
    assert response.json()["address"] == "Rua Nova, 42"

    response = await api_client.patch(url, headers=auth_headers, json={"address": clear_value})
    assert response.status_code == 200, response.text
    assert response.json()["address"] is None
    await db_session.commit()
    await db_session.refresh(patient)
    assert patient.address is None


async def test_patient_address_rejects_more_than_500_characters(
    api_client, patient, auth_headers
):
    response = await api_client.post(
        "/api/v1/patients", headers=auth_headers,
        json={"name": "Paciente teste", "birthDate": "2020-01-01", "diagnosisKeys": ["tea"], "address": "x" * 501},
    )
    assert response.status_code == 422
    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}", headers=auth_headers, json={"address": "x" * 501},
    )
    assert response.status_code == 422
