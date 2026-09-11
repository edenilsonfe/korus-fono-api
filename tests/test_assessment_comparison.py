"""Reassessment comparison: same-protocol deltas between completed assessments."""

from datetime import UTC, date, datetime

from app.core.security import create_access_token, hash_password
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.assessment_comparison import extract_numeric_metrics

BASE_BODY = {
    "protocolId": "portage",
    "date": "2026-01-10",
    "result": "Atraso leve",
    "percentage": 40,
    "scores": {
        "domains": {"linguagem": 4, "motor": 6, "social": 5},
        "total": 15,
        "summary": "Avaliação inicial",
    },
    "answers": {"1": "nao", "2": "sim", "3": "as vezes"},
}

TARGET_BODY = {
    **BASE_BODY,
    "date": "2026-06-10",
    "result": "Dentro do esperado",
    "percentage": 68,
    "scores": {
        "domains": {"linguagem": 7, "motor": 6, "social": 9},
        "total": 22,
        "summary": "Reavaliação",
    },
    "answers": {"1": "sim", "2": "sim", "3": "sempre"},
}


async def _create_assessment(api_client, auth_headers, patient, body):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_compare_returns_metric_deltas(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    target = await _create_assessment(api_client, auth_headers, patient, TARGET_BODY)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": base["id"], "targetId": target["id"]},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["protocolId"] == "portage"
    assert data["percentageDelta"] == 28
    assert data["base"]["date"] == "2026-01-10"
    assert data["target"]["date"] == "2026-06-10"
    metrics = {metric["key"]: metric for metric in data["metrics"]}
    assert metrics["linguagem"]["base"] == 4
    assert metrics["linguagem"]["target"] == 7
    assert metrics["linguagem"]["delta"] == 3
    assert metrics["motor"]["delta"] == 0
    assert metrics["total"]["delta"] == 7
    assert data["answersChanged"] == 2
    assert data["answersTotal"] == 3
    changed = {item["key"]: item for item in data["changedItems"]}
    assert changed["1"] == {"key": "1", "base": "nao", "target": "sim"}
    assert "28" in data["summary"]


async def test_compare_normalizes_chronological_order(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    target = await _create_assessment(api_client, auth_headers, patient, TARGET_BODY)

    # Swapped ids: base receives the newer assessment; server still answers oldest -> newest.
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": target["id"], "targetId": base["id"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["base"]["date"] == "2026-01-10"
    assert data["target"]["date"] == "2026-06-10"
    assert data["percentageDelta"] == 28


async def test_compare_requires_same_protocol(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    other_protocol = await _create_assessment(
        api_client,
        auth_headers,
        patient,
        {**BASE_BODY, "protocolId": "vanderbilt", "scores": {"total": 5}},
    )
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": base["id"], "targetId": other_protocol["id"]},
    )
    assert response.status_code == 400
    assert "mesmo protocolo" in response.json()["detail"]


async def test_compare_rejects_same_assessment(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": base["id"], "targetId": base["id"]},
    )
    assert response.status_code == 400
    assert "duas avaliações" in response.json()["detail"]


async def test_compare_rejects_drafts(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    draft = await api_client.put(
        f"/api/v1/patients/{patient.id}/assessments/drafts/portage",
        headers=auth_headers,
        json={"answers": {"1": "sim"}},
    )
    assert draft.status_code == 200
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": base["id"], "targetId": draft.json()["id"]},
    )
    assert response.status_code == 400
    assert "concluídas" in response.json()["detail"]


async def test_compare_missing_assessment_returns_404(api_client, auth_headers, patient):
    base = await _create_assessment(api_client, auth_headers, patient, BASE_BODY)
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={
            "baseId": base["id"],
            "targetId": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert response.status_code == 404


async def test_compare_denies_foreign_patient(api_client, db_session, patient):
    other = Professional(
        email="other-branding@example.com",
        password_hash=hash_password("testpass123"),
        name="Dr. Outro",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999990001",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.flush()
    foreign_patient = Patient(
        professional_id=other.id,
        name="Paciente Alheio",
        birth_date=date(2021, 1, 1),
        diagnosis_keys=["tea"],
        status="ativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign_patient)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=headers,
        params={
            "baseId": "00000000-0000-0000-0000-000000000000",
            "targetId": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert response.status_code == 404


def test_extract_numeric_metrics_shapes():
    assert extract_numeric_metrics(
        {"domains": {"a": 1, "b": {"score": 2.5}}, "total": 3, "summary": "x"}
    ) == {"a": 1.0, "b": 2.5, "total": 3.0}
    assert extract_numeric_metrics({"total": "12", "percentage": 33}) == {"total": 12.0}
    assert extract_numeric_metrics(None) == {}
    assert extract_numeric_metrics({"domains": "invalid"}) == {}
