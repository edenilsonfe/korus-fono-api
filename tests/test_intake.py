from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.anamnese import AnamneseEntry
from app.models.caregiver import Caregiver
from app.models.intake import IntakeRequest
from app.models.intake import IntakeGrant
from app.models.professional import Professional
from app.core.security import create_access_token, hash_password
from app.services.entitlement_service import EntitlementService


@pytest.fixture(autouse=True)
def intake_guards(monkeypatch):
    async def enabled(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.intake_service._workflow_enabled", enabled)
    monkeypatch.setattr("app.api.v1.intake.clinical_public_rate_limit.enforce_public_rate_limit", lambda **_kwargs: None)


@pytest.mark.asyncio
async def test_intake_draft_submit_and_revoke_returns_generic_410(api_client, auth_headers, patient, professional, db_session):
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    created = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json={"caregiverId": str(caregiver.id)})
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    issued = await api_client.post(
        f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/grants",
        headers=auth_headers,
        json={"authorizedAt": datetime.now(UTC).isoformat(), "reviewed": True},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["url"].split("#token=", 1)[1]
    saved = await api_client.patch("/api/v1/intake-responses", headers={"X-Intake-Token": token}, json={"expectedVersion": 1, "responses": {"reasonForReferral": {"value": "Fala", "notKnown": False}}})
    assert saved.status_code == 200, saved.text
    submitted = await api_client.post("/api/v1/intake-responses/submit", headers={"X-Intake-Token": token}, json={"expectedVersion": 2, "commandKey": "submit-1"})
    assert submitted.status_code == 200, submitted.text
    revoked = await api_client.delete(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/grants/{issued.json()['id']}", headers=auth_headers)
    assert revoked.status_code == 204
    gone = await api_client.get("/api/v1/intake-responses", headers={"X-Intake-Token": token})
    assert gone.status_code == 410
    assert "no-store" in gone.headers["cache-control"]


@pytest.mark.asyncio
async def test_only_one_open_request_per_caregiver(api_client, auth_headers, patient, db_session):
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    payload = {"caregiverId": str(caregiver.id)}
    first = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json=payload)
    second = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_review_does_not_modify_completed_anamnese(api_client, auth_headers, patient, db_session):
    db_session.add(AnamneseEntry(patient_id=patient.id, section="Profissional", value="Original"))
    patient.anamnese_status = "completed"
    patient.anamnese_completed_at = datetime.now(UTC)
    await db_session.commit()
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    created = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json={"caregiverId": str(caregiver.id)})
    request_id = created.json()["id"]
    fingerprint = (await api_client.get(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}", headers=auth_headers)).json()["anamneseFingerprint"]
    # Private tests use the service-level state to avoid relying on a public URL in this invariant.
    item = await db_session.scalar(select(IntakeRequest).where(IntakeRequest.id == UUID(request_id)))
    item.status = "submitted"
    item.responses = {"reasonForReferral": {"value": "Novo", "notKnown": False}}
    item.version = 1
    await db_session.commit()
    reviewed = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/review", headers=auth_headers, json={"expectedVersion": 1, "anamneseFingerprint": fingerprint, "selectedFields": ["reasonForReferral"], "commandKey": "review-1"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["selectedFields"] == []
    entries = (await db_session.execute(select(AnamneseEntry).where(AnamneseEntry.patient_id == patient.id))).scalars().all()
    assert [(entry.section, entry.value) for entry in entries] == [("Profissional", "Original")]


@pytest.mark.asyncio
async def test_intake_file_rejects_mime_mismatch_and_accepts_pdf(api_client, auth_headers, patient, db_session, monkeypatch):
    async def fake_upload(*_args, **_kwargs):
        return _args[0] if _args else ""

    monkeypatch.setattr("app.services.intake_service.storage_service.upload", fake_upload)
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    created = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json={"caregiverId": str(caregiver.id)})
    request_id = created.json()["id"]
    issued = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/grants", headers=auth_headers, json={"authorizedAt": datetime.now(UTC).isoformat(), "reviewed": True})
    token = issued.json()["url"].split("#token=", 1)[1]
    invalid = await api_client.post("/api/v1/intake-responses/files", headers={"X-Intake-Token": token}, files={"file": ("note.pdf", b"not a pdf", "application/pdf")})
    assert invalid.status_code == 400
    valid = await api_client.post("/api/v1/intake-responses/files", headers={"X-Intake-Token": token}, files={"file": ("note.pdf", b"%PDF-1.4\n", "application/pdf")})
    assert valid.status_code == 201, valid.text
    assert valid.json()["contentType"] == "application/pdf"


@pytest.mark.asyncio
async def test_intake_private_acl_expired_token_and_file_scope(api_client, auth_headers, patient, db_session):
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    created = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json={"caregiverId": str(caregiver.id)})
    request_id = created.json()["id"]
    issued = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/grants", headers=auth_headers, json={"authorizedAt": datetime.now(UTC).isoformat(), "reviewed": True})
    token = issued.json()["url"].split("#token=", 1)[1]

    other = Professional(email="other-intake@example.com", password_hash=hash_password("testpass123"), name="Outra", email_verified_at=datetime.now(UTC))
    db_session.add(other)
    await db_session.commit()
    other_response = await api_client.get(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}", headers={"Authorization": f"Bearer {create_access_token(other.id)}"})
    assert other_response.status_code == 404

    grant = await db_session.scalar(select(IntakeGrant).where(IntakeGrant.intake_request_id == UUID(request_id)))
    grant.created_at = datetime.now(UTC) - timedelta(days=2)
    grant.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    expired = await api_client.get("/api/v1/intake-responses", headers={"X-Intake-Token": token})
    assert expired.status_code == 410


@pytest.mark.asyncio
async def test_intake_public_write_checks_entitlement(api_client, auth_headers, patient, db_session, monkeypatch):
    caregiver = await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))
    created = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests", headers=auth_headers, json={"caregiverId": str(caregiver.id)})
    request_id = created.json()["id"]
    issued = await api_client.post(f"/api/v1/patients/{patient.id}/intake-requests/{request_id}/grants", headers=auth_headers, json={"authorizedAt": datetime.now(UTC).isoformat(), "reviewed": True})
    token = issued.json()["url"].split("#token=", 1)[1]
    professional = await db_session.get(Professional, patient.professional_id)
    professional.signup_payment_required = True
    await db_session.commit()

    async def entitlement_only(db, owner_id):
        owner = await db.get(Professional, owner_id)
        await EntitlementService(db).ensure_write_allowed(owner)

    monkeypatch.setattr("app.services.intake_service._workflow_enabled", entitlement_only)
    result = await api_client.patch("/api/v1/intake-responses", headers={"X-Intake-Token": token}, json={"expectedVersion": 1, "responses": {"reasonForReferral": {"value": "Bloqueado", "notKnown": False}}})
    assert result.status_code == 403
