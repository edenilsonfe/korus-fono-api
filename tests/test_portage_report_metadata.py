"""Portage report context survives persistence and remains scoped to its owner."""

from datetime import UTC, datetime

from app.core.security import create_access_token, hash_password
from app.models.professional import Professional


async def test_portage_report_context_roundtrip_and_isolation(
    api_client, auth_headers, patient, db_session
):
    metadata = {
        "age_band": "3-4",
        "application_mode": "single_band",
        "selected_domains": ["linguagem"],
        "chronological_age_months": 43,
        "clinical_synthesis": "Habilidades observadas em contexto lúdico.",
    }
    body = {
        "protocolId": "portage",
        "date": "2026-09-26",
        "result": "Registro descritivo dos domínios avaliados.",
        "answers": {"1": "sim", "2": "na"},
        "metadata": metadata,
    }
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments", headers=auth_headers, json=body
    )
    assert created.status_code == 201, created.text
    assert created.json()["metadata"] == metadata
    db_session.expire_all()
    history = await api_client.get("/api/v1/assessments", headers=auth_headers)
    assert history.status_code == 200, history.text
    saved = next(item for item in history.json()["items"] if item["id"] == created.json()["id"])
    assert saved["metadata"] == metadata
    assert saved["date"] == "2026-09-26"
    assert saved["answers"] == body["answers"]

    other = Professional(
        email="portage-other@example.com", password_hash=hash_password("testpass123"),
        name="Outro profissional", specialty_key="fono", specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {create_access_token(str(other.id))}"}
    other_history = await api_client.get("/api/v1/assessments", headers=other_headers)
    assert other_history.status_code == 200, other_history.text
    assert all(item["id"] != saved["id"] for item in other_history.json()["items"])
