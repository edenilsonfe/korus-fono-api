from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
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
        "version": 2,
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
            "configuredService": False,
            "createdRealPatient": False,
        },
    }


async def test_activation_recria_demo_ausente_de_forma_idempotente(
    api_client, db_session, professional, auth_headers
):
    first = await api_client.get("/api/v1/me/activation", headers=auth_headers)
    second = await api_client.get("/api/v1/me/activation", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["demoPatientId"] == second.json()["demoPatientId"]
    demo_rows = (
        await db_session.execute(
            select(Patient).where(
                Patient.professional_id == professional.id,
                Patient.is_demo.is_(True),
            )
        )
    ).scalars().all()
    assert len(demo_rows) == 1


async def test_demo_nao_pode_ser_excluido_durante_onboarding(
    api_client, db_session, professional, auth_headers
):
    demo = await _demo_patient(db_session, professional)

    response = await api_client.delete(f"/api/v1/patients/{demo.id}", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "O paciente demonstração faz parte dos primeiros passos e não pode ser removido agora"
    )


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


async def test_existing_professional_with_real_patient_resumes_at_service_configuration(
    api_client, db_session, professional, auth_headers
):
    db_session.add(
        Patient(
            professional_id=professional.id,
            name="Paciente já cadastrado",
            birth_date=date(2021, 5, 20),
            diagnosis_keys=[],
            status="avaliacao",
            start_date=date.today(),
            avatar_color="oklch(0.58 0.12 205)",
            is_demo=False,
        )
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/me/activation", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["demoPatientId"] is None
    assert response.json()["steps"] == {
        "viewedDemoPatient": True,
        "completedDemoAssessment": True,
        "viewedDemoResult": True,
        "createdDemoReport": True,
        "configuredService": False,
        "createdRealPatient": True,
    }
    assert response.json()["isComplete"] is False
    assert response.json()["nextStep"] == "configure_service"


async def test_activation_requires_a_configured_service_and_real_patient_to_complete(
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
    assert result_view.json()["steps"]["configuredService"] is False
    assert result_view.json()["nextStep"] == "configure_service"

    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={
            "name": "Terapia fonoaudiológica",
            "duration": 50,
            "priceCents": 18_000,
        },
    )
    assert service.status_code == 201, service.text

    configured = await api_client.get("/api/v1/me/activation", headers=auth_headers)
    assert configured.status_code == 200
    assert configured.json()["steps"]["configuredService"] is True
    assert configured.json()["nextStep"] == "create_real_patient"

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
    assert completed.json()["steps"]["configuredService"] is True
    assert completed.json()["steps"]["createdRealPatient"] is True
    assert completed.json()["isComplete"] is True
    assert completed.json()["nextStep"] == "completed"
    assert completed.json()["completedAt"] is not None
    assert datetime.fromisoformat(completed.json()["completedAt"].replace("Z", "+00:00")) >= now
