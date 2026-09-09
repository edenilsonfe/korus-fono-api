from datetime import UTC, datetime

from app.core.security import create_access_token, hash_password
from app.models.care_team import PatientCareTeamMember, PatientSharingConsentEvent
from app.models.feature_flag import FeatureFlag
from app.models.professional import Professional


def _headers(professional: Professional) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_access_token(professional.id, professional.token_version)}"
        )
    }


async def _active_member(db_session, patient, owner, *, role="practitioner"):
    member = Professional(
        email=f"{role}@shared-clinical.test",
        password_hash=hash_password("testpass123"),
        name=f"Dra. {role.title()}",
        specialty_key="psico",
        specialty="Psicologia",
        council="CRP 00/00000",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(member)
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
            professional_id=member.id,
            role=role,
            status="active",
            invited_by_professional_id=owner.id,
            consent_event_id=consent.id,
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await db_session.refresh(member)
    return member


async def test_shared_member_reads_chart_and_creates_only_authored_clinical_records(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
):
    db_session.add(
        FeatureFlag(
            key="multidisciplinary_aba",
            description="Equipe multiprofissional e programas ABA",
            enabled_global=True,
        )
    )
    await db_session.commit()
    practitioner = await _active_member(db_session, patient, professional)
    practitioner_headers = _headers(practitioner)

    owner_session = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=auth_headers,
        json={"type": "Fonoaudiologia", "objectives": ["Comunicação"]},
    )
    assert owner_session.status_code == 201, owner_session.text

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=practitioner_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["therapist"] == professional.name

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/sessions/{owner_session.json()['id']}",
        headers=practitioner_headers,
        json={"notes": "tentativa de sobrescrita"},
    )
    assert response.status_code == 404
    member_session = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=practitioner_headers,
        json={"type": "ABA", "objectives": ["Solicitar ajuda"]},
    )
    assert member_session.status_code == 201, member_session.text
    assert member_session.json()["therapist"] == practitioner.name

    evolution = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=practitioner_headers,
        json={
            "sessionId": member_session.json()["id"],
            "title": "Evolução compartilhada",
            "content": "Registro clínico sintético para teste.",
        },
    )
    assert evolution.status_code == 201, evolution.text
    assert evolution.json()["professional"] == practitioner.name

    goal = await api_client.post(
        f"/api/v1/patients/{patient.id}/goals",
        headers=practitioner_headers,
        json={"title": "Comunicação funcional", "area": "Linguagem"},
    )
    assert goal.status_code == 201, goal.text
    assert goal.json()["professional"] == practitioner.name

    assessment = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=practitioner_headers,
        json={
            "protocolId": "proc",
            "result": "Observação concluída",
            "percentage": 70,
            "interpretation": "Registro sintético para teste.",
        },
    )
    assert assessment.status_code == 201, assessment.text
    assert assessment.json()["professional"] == practitioner.name

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    detail = response.json()
    assert {item["therapist"] for item in detail["sessions"]} == {
        professional.name,
        practitioner.name,
    }
    assert detail["goals"][0]["professional"] == practitioner.name
    assert detail["assessments"][0]["professional"] == practitioner.name

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()[0]["professional"] == practitioner.name

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/timeline",
        headers=practitioner_headers,
    )
    assert response.status_code == 200
    assert any(item["type"] == "evolucao" for item in response.json())

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/anamnese",
        headers=practitioner_headers,
    )
    assert response.status_code == 200

    response = await api_client.put(
        f"/api/v1/patients/{patient.id}/anamnese",
        headers=practitioner_headers,
        json={"entries": [{"section": "Queixa", "value": "Não sobrescrever"}]},
    )
    assert response.status_code == 404


async def test_patient_with_clinical_history_must_be_inactivated_not_deleted(
    api_client,
    auth_headers,
    patient,
):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=auth_headers,
        json={"type": "Fonoaudiologia", "objectives": ["Comunicação"]},
    )
    assert response.status_code == 201, response.text

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}", headers=auth_headers
    )
    assert response.status_code == 409
    assert "histórico clínico" in response.json()["detail"]


async def test_shared_member_can_start_own_battery(
    api_client,
    db_session,
    patient,
    professional,
):
    db_session.add(
        FeatureFlag(
            key="multidisciplinary_aba",
            description="Equipe multiprofissional e programas ABA",
            enabled_global=True,
        )
    )
    await db_session.commit()
    practitioner = await _active_member(db_session, patient, professional)

    response = await api_client.post(
        "/api/v1/batteries",
        headers=_headers(practitioner),
        json={
            "instrumentSlug": "abfw",
            "patientId": str(patient.id),
            "moduleSlugs": ["fonologia-nomeacao"],
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["professionalId"] == str(practitioner.id)

    response = await api_client.get(
        f"/api/v1/batteries?patientId={patient.id}",
        headers=_headers(professional),
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1

    response = await api_client.get(
        f"/api/v1/batteries/{response.json()['items'][0]['id']}",
        headers=_headers(professional),
    )
    assert response.status_code == 200, response.text


async def test_shared_member_can_schedule_own_appointment(
    api_client,
    db_session,
    patient,
    professional,
):
    db_session.add(
        FeatureFlag(
            key="multidisciplinary_aba",
            description="Equipe multiprofissional e programas ABA",
            enabled_global=True,
        )
    )
    await db_session.commit()
    practitioner = await _active_member(db_session, patient, professional)

    response = await api_client.post(
        "/api/v1/appointments",
        headers=_headers(practitioner),
        json={
            "patientId": str(patient.id),
            "date": "2027-01-10",
            "time": "10:00",
            "type": "ABA",
            "duration": 50,
            "status": "pendente",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["therapist"] == practitioner.name
