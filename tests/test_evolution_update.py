from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.care_team import PatientCareTeamMember, PatientSharingConsentEvent
from app.models.evolution import Evolution
from app.models.feature_flag import FeatureFlag
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.timeline import TimelineEvent


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


async def test_update_evolution_updates_record_and_timeline(
    api_client, auth_headers, patient: Patient, db_session
):
    session = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=auth_headers,
        json={"type": "Fonoaudiologia", "objectives": []},
    )
    assert session.status_code == 201, session.text
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=auth_headers,
        json={
            "title": "Antes",
            "content": "Conteúdo original",
            "sessionId": session.json()["id"],
        },
    )
    assert created.status_code == 201, created.text
    evolution_id = created.json()["id"]
    original_date = created.json()["date"]
    original_session_id = created.json()["sessionId"]
    assert created.json()["canEdit"] is True

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/evolutions/{evolution_id}",
        headers=auth_headers,
        json={"title": "Depois", "content": "  Conteúdo atualizado  "},
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Depois"
    assert response.json()["content"] == "Conteúdo atualizado"
    assert _utc_datetime(response.json()["date"]) == _utc_datetime(original_date)
    assert response.json()["sessionId"] == original_session_id
    assert response.json()["canEdit"] is True

    listed = await api_client.get(
        f"/api/v1/patients/{patient.id}/evolutions", headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    persisted = next(item for item in listed.json() if item["id"] == evolution_id)
    assert persisted["title"] == "Depois"
    assert persisted["content"] == "Conteúdo atualizado"
    assert _utc_datetime(persisted["date"]) == _utc_datetime(original_date)
    assert persisted["sessionId"] == original_session_id
    assert persisted["canEdit"] is True

    evolution = await db_session.get(Evolution, UUID(evolution_id))
    timeline = await db_session.scalar(
        select(TimelineEvent).where(TimelineEvent.source_id == evolution.id)
    )
    assert evolution.title == timeline.title == "Depois"
    assert evolution.content == timeline.description == "Conteúdo atualizado"


async def test_evolution_can_edit_is_limited_to_its_author_on_shared_patient(
    api_client, auth_headers, patient: Patient, professional: Professional, db_session
):
    db_session.add(
        FeatureFlag(
            key="multidisciplinary_aba",
            description="Equipe multiprofissional e programas ABA",
            enabled_global=True,
        )
    )
    await db_session.commit()

    member = Professional(
        email="evolution-member@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
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
        recorded_by_professional_id=professional.id,
        recorded_at=datetime.now(UTC),
    )
    db_session.add(consent)
    await db_session.flush()
    db_session.add(
        PatientCareTeamMember(
            patient_id=patient.id,
            professional_id=member.id,
            role="practitioner",
            status="active",
            invited_by_professional_id=professional.id,
            consent_event_id=consent.id,
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await db_session.refresh(member)
    member_headers = {
        "Authorization": f"Bearer {create_access_token(member.id, member.token_version)}"
    }

    owner_evolution = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=auth_headers,
        json={"content": "Registro do proprietário."},
    )
    assert owner_evolution.status_code == 201, owner_evolution.text
    owner_evolution_id = owner_evolution.json()["id"]
    assert owner_evolution.json()["canEdit"] is True

    member_list = await api_client.get(
        f"/api/v1/patients/{patient.id}/evolutions", headers=member_headers
    )
    assert member_list.status_code == 200, member_list.text
    owner_seen = next(
        item for item in member_list.json() if item["id"] == owner_evolution_id
    )
    assert owner_seen["canEdit"] is False

    denied = await api_client.patch(
        f"/api/v1/patients/{patient.id}/evolutions/{owner_evolution_id}",
        headers=member_headers,
        json={"title": "Tentativa", "content": "Não deve alterar."},
    )
    assert denied.status_code == 404

    member_evolution = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=member_headers,
        json={"content": "Registro do profissional compartilhado."},
    )
    assert member_evolution.status_code == 201, member_evolution.text
    member_evolution_id = member_evolution.json()["id"]
    assert member_evolution.json()["canEdit"] is True

    member_list = await api_client.get(
        f"/api/v1/patients/{patient.id}/evolutions", headers=member_headers
    )
    assert member_list.status_code == 200, member_list.text
    member_seen = next(
        item for item in member_list.json() if item["id"] == member_evolution_id
    )
    assert member_seen["canEdit"] is True

    owner_list = await api_client.get(
        f"/api/v1/patients/{patient.id}/evolutions", headers=auth_headers
    )
    assert owner_list.status_code == 200, owner_list.text
    owner_seen_member = next(
        item for item in owner_list.json() if item["id"] == member_evolution_id
    )
    assert owner_seen_member["canEdit"] is False
