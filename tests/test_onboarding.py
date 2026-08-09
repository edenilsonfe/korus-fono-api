from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ai import AIReport
from app.models.assessment import Assessment
from app.models.patient import Patient


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


async def _demo_patient(db_session, professional) -> Patient:
    patient = Patient(
        professional_id=professional.id,
        name="Paciente demonstração",
        birth_date=date(2024, 12, 1),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
        is_demo=True,
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


async def test_activation_starts_with_server_derived_demo_state(
    api_client, db_session, professional, auth_headers
):
    demo = await _demo_patient(db_session, professional)

    response = await api_client.get("/api/v1/me/activation", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "demoPatientId": str(demo.id),
        "startedAt": professional.created_at.isoformat().replace("+00:00", "Z"),
        "completedAt": None,
        "dismissedUntil": None,
        "isComplete": False,
        "nextStep": "view_demo_patient",
        "steps": {
            "viewedDemoPatient": False,
            "completedDemoAssessment": False,
            "viewedDemoResult": False,
            "createdDemoReport": False,
            "createdRealPatient": False,
        },
    }


async def test_activation_persists_view_postpone_and_resume(
    api_client, db_session, professional, auth_headers
):
    await _demo_patient(db_session, professional)

    viewed = await api_client.patch(
        "/api/v1/me/activation",
        headers=auth_headers,
        json={"action": "viewed_demo_patient"},
    )
    assert viewed.status_code == 200
    assert viewed.json()["steps"]["viewedDemoPatient"] is True
    assert viewed.json()["nextStep"] == "complete_demo_assessment"

    postponed = await api_client.patch(
        "/api/v1/me/activation",
        headers=auth_headers,
        json={"action": "postpone"},
    )
    assert postponed.status_code == 200
    assert postponed.json()["dismissedUntil"] is not None

    resumed = await api_client.patch(
        "/api/v1/me/activation",
        headers=auth_headers,
        json={"action": "resume"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["dismissedUntil"] is None


async def test_result_view_requires_a_completed_demo_assessment(
    api_client, db_session, professional, auth_headers
):
    await _demo_patient(db_session, professional)

    response = await api_client.patch(
        "/api/v1/me/activation",
        headers=auth_headers,
        json={"action": "viewed_demo_result"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conclua a avaliação demonstrativa antes de ver o resultado"


async def test_activation_derives_clinical_value_and_completes_with_real_patient(
    api_client, db_session, professional, auth_headers
):
    demo = await _demo_patient(db_session, professional)
    now = datetime.now(UTC)
    db_session.add(
        Assessment(
            patient_id=demo.id,
            professional_id=professional.id,
            protocol_id="mchat",
            date=date.today(),
            result="Baixo risco",
            percentage=90,
            interpretation="Resultado demonstrativo",
            fields=[],
            answers={},
            status="completed",
        )
    )
    db_session.add(
        AIReport(
            professional_id=professional.id,
            patient_id=demo.id,
            type="clinical",
            date=date.today(),
            preview="Rascunho demonstrativo",
            content="Conteúdo",
            status="draft",
        )
    )
    await db_session.commit()

    result_view = await api_client.patch(
        "/api/v1/me/activation",
        headers=auth_headers,
        json={"action": "viewed_demo_result"},
    )
    assert result_view.status_code == 200
    assert result_view.json()["steps"]["completedDemoAssessment"] is True
    assert result_view.json()["steps"]["viewedDemoResult"] is True
    assert result_view.json()["steps"]["createdDemoReport"] is True
    assert result_view.json()["nextStep"] == "create_real_patient"

    created = await api_client.post(
        "/api/v1/patients",
        headers=auth_headers,
        json={
            "name": "Primeiro paciente real",
            "birthDate": "2021-05-20",
            "diagnosisKeys": ["tea"],
            "status": "avaliacao",
            "guardians": [],
        },
    )
    assert created.status_code == 201
    assert created.json()["isDemo"] is False

    completed = await api_client.get("/api/v1/me/activation", headers=auth_headers)
    assert completed.status_code == 200
    assert completed.json()["steps"]["createdRealPatient"] is True
    assert completed.json()["isComplete"] is True
    assert completed.json()["nextStep"] == "completed"
    assert completed.json()["completedAt"] is not None
    assert datetime.fromisoformat(completed.json()["completedAt"].replace("Z", "+00:00")) >= now
