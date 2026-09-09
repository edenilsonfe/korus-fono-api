from datetime import UTC, datetime
from uuid import uuid4

from app.core.security import create_access_token, hash_password
from app.models.care_team import PatientCareTeamMember, PatientSharingConsentEvent
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session


def _headers(professional: Professional) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_access_token(professional.id, professional.token_version)}"
        )
    }


async def _enable_feature(db_session) -> None:
    db_session.add(
        FeatureFlag(
            key="multidisciplinary_aba",
            description="Equipe multiprofissional e programas ABA",
            enabled_global=True,
        )
    )
    await db_session.commit()


async def _team_member(
    db_session,
    patient: Patient,
    owner: Professional,
    *,
    email: str,
    role: str,
) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=f"Dra. {role.title()}",
        specialty_key="psico",
        specialty="Psicologia",
        council="CRP 00/00000",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.flush()
    consent = PatientSharingConsentEvent(
        patient_id=patient.id,
        decision="granted",
        policy_version="2026-09-09",
        recorded_by_professional_id=owner.id,
        recorded_at=datetime.now(UTC),
    )
    db_session.add(consent)
    await db_session.flush()
    db_session.add(
        PatientCareTeamMember(
            patient_id=patient.id,
            professional_id=professional.id,
            role=role,
            status="active",
            invited_by_professional_id=owner.id,
            consent_event_id=consent.id,
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


async def _session(db_session, patient: Patient, professional: Professional) -> Session:
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime.now(UTC),
        duration=50,
        type="ABA",
        objectives=[],
        notes="",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


def _program_body(goal_id: str | None = None) -> dict:
    return {
        "goalId": goal_id,
        "title": "Solicitar ajuda",
        "operationalDefinition": "Pedir ajuda com frase funcional sem agressão.",
        "teachingStrategy": "Ensino incidental com atraso de dica.",
        "masteryPercent": 80,
        "masteryConsecutiveSessions": 2,
    }


async def test_program_lifecycle_freezes_active_definition_and_preserves_goal(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
):
    await _enable_feature(db_session)
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Comunicação funcional",
        area="Linguagem",
        progress=25,
        start_date=datetime.now(UTC).date(),
        status="Inicial",
    )
    db_session.add(goal)
    await db_session.commit()

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs",
        headers=auth_headers,
        json=_program_body(str(goal.id)),
    )
    assert response.status_code == 201, response.text
    program = response.json()
    assert program["approach"] == "aba"
    assert program["status"] == "draft"
    assert program["masteryEligible"] is False

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program['id']}",
        headers=auth_headers,
        json={"status": "active"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["activatedAt"] is not None

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program['id']}",
        headers=auth_headers,
        json={"status": "mastered"},
    )
    assert response.status_code == 409
    assert "dados aprovados" in response.json()["detail"]

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program['id']}",
        headers=auth_headers,
        json={"operationalDefinition": "Outra definição"},
    )
    assert response.status_code == 409
    assert "novo programa" in response.json()["detail"]

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program['id']}",
        headers=auth_headers,
        json={"status": "draft"},
    )
    assert response.status_code == 409

    await db_session.refresh(goal)
    assert goal.progress == 25

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}", headers=auth_headers
    )
    assert response.status_code == 409
    assert "altere o status para inativo" in response.json()["detail"]


async def test_measurements_are_idempotent_validated_and_patient_scoped(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
):
    await _enable_feature(db_session)
    session = await _session(db_session, patient, professional)
    program_response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs",
        headers=auth_headers,
        json=_program_body(),
    )
    program_id = program_response.json()["id"]
    await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}",
        headers=auth_headers,
        json={"status": "active"},
    )

    inconsistent = {
        "clientRecordId": str(uuid4()),
        "sessionId": str(session.id),
        "participationStatus": "participated",
        "opportunities": 5,
        "independent": 2,
        "prompted": 1,
        "incorrect": 1,
        "noResponse": 0,
    }
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=inconsistent,
    )
    assert response.status_code == 422

    declined = inconsistent | {
        "clientRecordId": str(uuid4()),
        "participationStatus": "declined",
        "opportunities": 1,
        "independent": 1,
        "prompted": 0,
        "incorrect": 0,
    }
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=declined,
    )
    assert response.status_code == 422

    other_patient = Patient(
        professional_id=professional.id,
        name="Outro paciente",
        birth_date=patient.birth_date,
        diagnosis_keys=[],
        status="ativo",
        start_date=patient.start_date,
        avatar_color=patient.avatar_color,
    )
    db_session.add(other_patient)
    await db_session.commit()
    other_session = await _session(db_session, other_patient, professional)
    body = inconsistent | {
        "clientRecordId": str(uuid4()),
        "sessionId": str(other_session.id),
        "opportunities": 5,
        "independent": 4,
        "prompted": 1,
        "incorrect": 0,
    }
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 404

    body["sessionId"] = str(session.id)
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["reviewStatus"] == "approved"

    retry = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=body,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first["id"]

    duplicate = body | {"clientRecordId": str(uuid4())}
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json=duplicate,
    )
    assert response.status_code == 409
    assert "mensuração vigente" in response.json()["detail"]

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.json()) == 1

    no_observation_session = await _session(db_session, patient, professional)
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=auth_headers,
        json={
            "clientRecordId": str(uuid4()),
            "sessionId": str(no_observation_session.id),
            "participationStatus": "not_observed",
            "opportunities": 0,
            "independent": 0,
            "prompted": 0,
            "incorrect": 0,
            "noResponse": 0,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["opportunities"] == 0


async def test_practitioner_submits_supervisor_reviews_and_mastery_is_manual(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
):
    await _enable_feature(db_session)
    practitioner = await _team_member(
        db_session,
        patient,
        professional,
        email="aplicadora@example.com",
        role="practitioner",
    )
    supervisor = await _team_member(
        db_session,
        patient,
        professional,
        email="supervisora@example.com",
        role="supervisor",
    )
    program_response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs",
        headers=auth_headers,
        json=_program_body(),
    )
    program_id = program_response.json()["id"]
    await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}",
        headers=auth_headers,
        json={"status": "active"},
    )
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs",
        headers=_headers(practitioner),
        json=_program_body(),
    )
    assert response.status_code == 404

    measurement_ids = []
    for _ in range(2):
        session = await _session(db_session, patient, practitioner)
        body = {
            "clientRecordId": str(uuid4()),
            "sessionId": str(session.id),
            "participationStatus": "participated",
            "opportunities": 5,
            "independent": 4,
            "prompted": 1,
            "incorrect": 0,
            "noResponse": 0,
            "promptCounts": {"verbal": 1},
        }
        response = await api_client.post(
            f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
            headers=_headers(practitioner),
            json=body,
        )
        assert response.status_code == 201, response.text
        assert response.json()["reviewStatus"] == "submitted"
        measurement_ids.append(response.json()["id"])

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}",
        headers=_headers(supervisor),
    )
    assert response.status_code == 200
    assert response.json()["masteryEligible"] is False

    for measurement_id in measurement_ids:
        response = await api_client.post(
            f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements/{measurement_id}/review",
            headers=_headers(supervisor),
            json={"action": "approve"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["reviewStatus"] == "approved"

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}",
        headers=_headers(supervisor),
    )
    assert response.json()["masteryEligible"] is True
    assert response.json()["status"] == "active"

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}",
        headers=_headers(supervisor),
        json={"status": "mastered"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "mastered"

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements/{measurement_ids[0]}/review",
        headers=_headers(supervisor),
        json={"action": "void"},
    )
    assert response.status_code == 422

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements/{measurement_ids[0]}/review",
        headers=_headers(supervisor),
        json={"action": "void", "reason": "Correção de transcrição"},
    )
    assert response.status_code == 200
    assert response.json()["reviewStatus"] == "voided"
    replaced = response.json()

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=_headers(practitioner),
        json={
            "clientRecordId": str(uuid4()),
            "sessionId": replaced["sessionId"],
            "participationStatus": "participated",
            "opportunities": 5,
            "independent": 4,
            "prompted": 1,
            "incorrect": 0,
            "noResponse": 0,
            "promptCounts": {"verbal": 1},
            "replacesMeasurementId": replaced["id"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["reviewStatus"] == "submitted"
    assert response.json()["replacesMeasurementId"] == replaced["id"]

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program_id}/measurements",
        headers=_headers(supervisor),
    )
    assert len(response.json()) == 3
    assert any(item["reviewStatus"] == "voided" for item in response.json())
