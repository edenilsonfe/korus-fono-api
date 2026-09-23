"""Pilot capabilities use the patient's owner and retain authorized archive access."""
import uuid

from app.core.security import create_access_token, hash_password
from app.core.utils import utcnow
from app.models.feature_flag import FeatureFlag, FeatureFlagOverride
from app.models.professional import Professional


async def test_flags_default_off_owner_override_and_cross_account_denied(api_client, auth_headers, professional, patient, db_session):
    path = f"/api/v1/patients/{patient.id}/clinical-workflows"
    initial = await api_client.get(path, headers=auth_headers)
    assert initial.status_code == 200
    assert initial.json()["patientIntakeEnabled"] is False
    assert initial.json()["canManageIntake"] is True
    db_session.add(FeatureFlag(key="patient_intake", description="Pilot", enabled_global=False))
    await db_session.flush()
    db_session.add(FeatureFlagOverride(flag_key="patient_intake", professional_id=professional.id, enabled=True))
    outsider = Professional(id=uuid.uuid4(), email="outside@example.test", password_hash=hash_password("test123456"), name="Outra conta", email_verified_at=utcnow())
    db_session.add(outsider)
    await db_session.commit()
    enabled = await api_client.get(path, headers=auth_headers)
    assert enabled.json()["patientIntakeEnabled"] is True
    assert enabled.json()["clinicalReviewsEnabled"] is False
    denied = await api_client.get(path, headers={"Authorization": f"Bearer {create_access_token(outsider.id)}"})
    assert denied.status_code == 404
