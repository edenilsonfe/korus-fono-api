from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.patient import Patient


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.parametrize("notes", [None, "", " \n ", "  Primeira observação\nSegunda observação  "])
async def test_create_patient_persists_optional_notes(
    api_client, db_session, auth_headers, notes
):
    body = {"name": "Paciente teste", "birthDate": "2020-01-01", "diagnosisKeys": ["tea"]}
    if notes is not None:
        body["notes"] = notes
    response = await api_client.post("/api/v1/patients", headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    expected = (notes.strip() or None) if notes is not None else None
    data = response.json()
    assert data["notes"] == expected
    await db_session.commit()
    saved = await db_session.get(Patient, UUID(data["id"]))
    await db_session.refresh(saved)
    assert saved.notes == expected

    detail = await api_client.get(f"/api/v1/patients/{data['id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["notes"] == expected
    listing = await api_client.get("/api/v1/patients", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    listed = next(item for item in listing.json()["items"] if item["id"] == data["id"])
    assert listed["notes"] == expected


@pytest.mark.parametrize("clear_value", [None, "", " \n "])
async def test_update_patient_changes_preserves_and_clears_notes(
    api_client, db_session, patient, auth_headers, clear_value
):
    url = f"/api/v1/patients/{patient.id}"
    expected = "Observação atualizada\nSegunda linha"
    response = await api_client.patch(
        url, headers=auth_headers, json={"notes": f"  {expected}  "}
    )
    assert response.status_code == 200, response.text
    assert response.json()["notes"] == expected
    await db_session.commit()
    await db_session.refresh(patient)
    assert patient.notes == expected

    response = await api_client.patch(url, headers=auth_headers, json={"address": "Rua Nova, 42"})
    assert response.status_code == 200, response.text
    assert response.json()["notes"] == expected

    response = await api_client.patch(url, headers=auth_headers, json={"notes": clear_value})
    assert response.status_code == 200, response.text
    assert response.json()["notes"] is None
    assert response.json()["address"] == "Rua Nova, 42"
    await db_session.commit()
    await db_session.refresh(patient)
    assert patient.notes is None


@pytest.mark.parametrize("length, status", [(5000, 201), (5001, 422)])
async def test_patient_notes_length_limit(api_client, patient, auth_headers, length, status):
    response = await api_client.post(
        "/api/v1/patients",
        headers=auth_headers,
        json={
            "name": "Paciente teste",
            "birthDate": "2020-01-01",
            "diagnosisKeys": ["tea"],
            "notes": "x" * length,
        },
    )
    assert response.status_code == status
    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}",
        headers=auth_headers,
        json={"notes": "x" * length},
    )
    assert response.status_code == (200 if status == 201 else 422)
