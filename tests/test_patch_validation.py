"""PATCH omission, explicit null and persistence across profile/clinical APIs."""

import pytest


@pytest.mark.parametrize("domain,field,attribute", [
    ("me", field, attribute) for field, attribute in [
        ("name", "name"), ("specialtyKey", "specialty_key"), ("council", "council"),
        ("phone", "phone"), ("avatarColor", "avatar_color"), ("billingAddress", "billing_address"),
        ("billingAddressNumber", "billing_address_number"), ("billingAddressComplement", "billing_address_complement"),
        ("billingProvince", "billing_province"), ("billingPostalCode", "billing_postal_code"),
    ]
] + [
    ("patient", "name", "name"), ("patient", "birthDate", "birth_date"),
    ("patient", "diagnosisKeys", "diagnosis_keys"), ("patient", "status", "status"),
] + [("session", field, field) for field in ["duration", "type", "objectives", "notes"]])
async def test_patch_rejects_null_and_preserves_existing_values(
    api_client, auth_headers, patient, professional, db_session, domain, field, attribute,
):
    if domain == "me":
        path, record = "/api/v1/me", professional
    elif domain == "patient":
        path, record = f"/api/v1/patients/{patient.id}", patient
    else:
        from uuid import UUID
        from app.models.session import Session
        created = await api_client.post(f"/api/v1/patients/{patient.id}/sessions", headers=auth_headers, json={})
        assert created.status_code == 201, created.text
        path = f"/api/v1/patients/{patient.id}/sessions/{created.json()['id']}"
        record = await db_session.get(Session, UUID(created.json()["id"]))
    previous = getattr(record, attribute)
    rejected = await api_client.patch(path, headers=auth_headers, json={field: None})
    assert rejected.status_code == 422, rejected.text
    omitted = await api_client.patch(path, headers=auth_headers, json={})
    assert omitted.status_code == 200, omitted.text
    await db_session.refresh(record)
    assert getattr(record, attribute) == previous


async def test_patient_patch_clears_nullable_fields_and_persists_other_changes(
    api_client, auth_headers, patient, db_session,
):
    path = f"/api/v1/patients/{patient.id}"
    response = await api_client.patch(path, headers=auth_headers,
                                     json={"address": "Rua de teste", "notes": "Observação"})
    assert response.status_code == 200
    response = await api_client.patch(path, headers=auth_headers,
                                     json={"address": None, "notes": None, "name": "Nome atualizado"})
    assert response.status_code == 200
    await db_session.refresh(patient)
    assert (patient.address, patient.notes, patient.name) == (None, None, "Nome atualizado")
