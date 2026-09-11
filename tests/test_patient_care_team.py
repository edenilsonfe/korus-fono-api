from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.care_team import PatientCareTeamMember
from app.models.caregiver import Caregiver
from app.models.feature_flag import FeatureFlag
from app.models.home_program import HomeProgramGrant
from app.models.intervention_program import InterventionProgram
from app.models.professional import Professional
from app.utils.token_hash import hash_token


async def _professional(db_session, *, email: str, name: str) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=name,
        specialty_key="psico",
        specialty="Psicologia",
        council="CRP 00/00000",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


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


async def test_invitation_requires_consent_and_grants_access_only_after_acceptance(
    api_client,
    auth_headers,
    db_session,
    patient,
    monkeypatch,
):
    await _enable_feature(db_session)
    invited = await _professional(
        db_session,
        email="convidada@example.com",
        name="Dra. Convidada",
    )
    raw_token = "convite-secreto-de-teste"
    monkeypatch.setattr(
        "app.services.care_team_service.secrets.token_urlsafe",
        lambda _size: raw_token,
    )
    invitation_body = {"email": invited.email, "role": "practitioner"}
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json=invitation_body,
    )
    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]
        == "Registre a autorização de compartilhamento antes de convidar"
    )

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201
    consent = response.json()
    assert consent["decision"] == "granted"
    assert consent["recordedByProfessionalId"]

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json=invitation_body,
    )
    assert response.status_code == 201
    invitation = response.json()
    assert invitation["status"] == "invited"
    assert invitation["role"] == "practitioner"
    assert "token" not in invitation
    assert raw_token not in response.text

    member = await db_session.scalar(
        select(PatientCareTeamMember).where(
            PatientCareTeamMember.id == UUID(invitation["id"])
        )
    )
    assert member is not None
    assert member.invite_token_hash == hash_token(raw_token)
    assert member.invite_token_hash != raw_token

    invited_headers = _headers(invited)
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/care-team",
        headers=invited_headers,
    )
    assert response.status_code == 404

    response = await api_client.post(
        "/api/v1/care-team/invitations/accept",
        headers=invited_headers,
        json={"token": raw_token},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/care-team",
        headers=invited_headers,
    )
    assert response.status_code == 200
    team = response.json()
    assert [item["role"] for item in team] == ["coordinator", "practitioner"]
    assert team[1]["professionalId"] == str(invited.id)

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}",
        headers=invited_headers,
    )
    assert response.status_code == 200
    shared_patient = response.json()
    assert shared_patient["access"]["role"] == "practitioner"
    assert shared_patient["access"]["isOwner"] is False
    assert shared_patient["address"] is None
    assert shared_patient["notes"] is None
    assert shared_patient["guardian"] == ""
    assert shared_patient["caregivers"] == []

    response = await api_client.get("/api/v1/patients", headers=invited_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["access"]["role"] == "practitioner"

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}",
        headers=invited_headers,
        json={"notes": "não autorizado"},
    )
    assert response.status_code == 404

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "withdrawn", "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/care-team",
        headers=invited_headers,
    )
    assert response.status_code == 404

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/access-events",
        headers=auth_headers,
        params={"page": 1, "limit": 100},
    )
    assert response.status_code == 200
    audit = response.json()
    assert audit["total"] == 6
    assert {item["action"] for item in audit["items"]} == {
        "sharing_consent_granted",
        "member_invited",
        "member_accepted",
        "member_revoked",
        "sharing_consent_withdrawn",
        "patient_record_opened",
    }
    assert raw_token not in response.text
    assert invited.email not in response.text


async def test_only_recipient_can_accept_and_token_is_single_use(
    api_client,
    auth_headers,
    db_session,
    patient,
    monkeypatch,
):
    await _enable_feature(db_session)
    invited = await _professional(
        db_session,
        email="destinataria@example.com",
        name="Dra. Destinatária",
    )
    stranger = await _professional(
        db_session,
        email="estranha@example.com",
        name="Dra. Estranha",
    )
    raw_token = "token-de-uso-unico"
    monkeypatch.setattr(
        "app.services.care_team_service.secrets.token_urlsafe",
        lambda _size: raw_token,
    )

    consent_response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert consent_response.status_code == 201, consent_response.text
    invitation_response = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json={"email": invited.email, "role": "supervisor"},
    )
    assert invitation_response.status_code == 201, invitation_response.text

    response = await api_client.post(
        "/api/v1/care-team/invitations/accept",
        headers=_headers(stranger),
        json={"token": raw_token},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Convite inválido ou expirado"

    response = await api_client.post(
        "/api/v1/care-team/invitations/accept",
        headers=_headers(invited),
        json={"token": raw_token},
    )
    assert response.status_code == 200

    response = await api_client.post(
        "/api/v1/care-team/invitations/accept",
        headers=_headers(invited),
        json={"token": raw_token},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Convite inválido ou expirado"


async def test_owner_can_change_role_and_revoke_but_member_cannot_manage_team(
    api_client,
    auth_headers,
    db_session,
    patient,
    monkeypatch,
):
    await _enable_feature(db_session)
    invited = await _professional(
        db_session,
        email="membro@example.com",
        name="Dra. Membro",
    )
    raw_token = "token-para-revogacao"
    monkeypatch.setattr(
        "app.services.care_team_service.secrets.token_urlsafe",
        lambda _size: raw_token,
    )

    consent_response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert consent_response.status_code == 201, consent_response.text
    invitation = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json={"email": invited.email, "role": "practitioner"},
    )
    assert invitation.status_code == 201, invitation.text
    invitation = invitation.json()
    invited_headers = _headers(invited)
    await api_client.post(
        "/api/v1/care-team/invitations/accept",
        headers=invited_headers,
        json={"token": raw_token},
    )

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/care-team/{invitation['id']}",
        headers=invited_headers,
        json={"role": "supervisor"},
    )
    assert response.status_code == 404

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/care-team/{invitation['id']}",
        headers=auth_headers,
        json={"role": "supervisor"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "supervisor"

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}/care-team/{invitation['id']}",
        headers=auth_headers,
        params={"reason": "Mudança da equipe assistencial"},
    )
    assert response.status_code == 204

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/care-team",
        headers=invited_headers,
    )
    assert response.status_code == 404


async def test_feature_flag_hides_team_and_sharing_history_blocks_patient_deletion(
    api_client,
    auth_headers,
    db_session,
    patient,
):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 404

    await _enable_feature(db_session)
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}",
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert "altere o status para inativo" in response.json()["detail"]


async def test_decline_is_single_use_and_invitation_respects_cooldown(
    api_client,
    auth_headers,
    db_session,
    patient,
    monkeypatch,
):
    await _enable_feature(db_session)
    invited = await _professional(
        db_session,
        email="recusa@example.com",
        name="Dra. Recusa",
    )
    raw_token = "token-para-recusar"
    monkeypatch.setattr(
        "app.services.care_team_service.secrets.token_urlsafe",
        lambda _size: raw_token,
    )
    await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    invitation = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json={"email": invited.email, "role": "supervisor"},
    )
    assert invitation.status_code == 201

    response = await api_client.post(
        (
            f"/api/v1/patients/{patient.id}/care-team/invitations/"
            f"{invitation.json()['id']}/resend"
        ),
        headers=auth_headers,
    )
    assert response.status_code == 429

    response = await api_client.post(
        "/api/v1/care-team/invitations/decline",
        headers=_headers(invited),
        json={"token": raw_token},
    )
    assert response.status_code == 204

    response = await api_client.post(
        "/api/v1/care-team/invitations/decline",
        headers=_headers(invited),
        json={"token": raw_token},
    )
    assert response.status_code == 400

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/care-team/invitations",
        headers=auth_headers,
        json={"email": invited.email, "role": "supervisor"},
    )
    assert response.status_code == 429


async def test_withdrawal_revokes_aba_derived_home_program_grants(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
):
    """F16: a retirada do consentimento invalida a derivação ABA do programa de
    casa — o grant ativo é revogado na mesma transação — e uma concessão
    posterior não ressuscita o link antigo."""
    await _enable_feature(db_session)
    consent = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "granted", "policyVersion": "2026-09-09"},
    )
    assert consent.status_code == 201
    aba = InterventionProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Ensino de tato",
        operational_definition="Nomeia figuras de animais.",
        teaching_strategy="Tentativas discretas.",
        mastery_percent=80,
        mastery_consecutive_sessions=3,
        status="active",
    )
    db_session.add(aba)
    await db_session.commit()
    await db_session.refresh(aba)

    starts_on = date.today()
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json={
            "title": "Rotina com ABA",
            "startsOn": starts_on.isoformat(),
            "endsOn": (starts_on + timedelta(days=13)).isoformat(),
            "tasks": [
                {
                    "title": "Nomear figuras",
                    "instructions": "Mostre os cartões.",
                    "dueOn": starts_on.isoformat(),
                    "interventionProgramId": str(aba.id),
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    program_id = created.json()["id"]
    published = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{program_id}/publish",
        headers=auth_headers,
        json={"expectedVersion": 1},
    )
    assert published.status_code == 200, published.text
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    grants_url = (
        f"/api/v1/patients/{patient.id}/home-programs/{program_id}/grants"
    )
    grant = await api_client.post(
        grants_url,
        headers=auth_headers,
        json={
            "caregiverId": str(caregiver.id),
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reviewed": True,
            },
        },
    )
    assert grant.status_code == 201, grant.text
    grant_id = UUID(grant.json()["id"])

    withdrawn = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": "withdrawn", "policyVersion": "2026-09-09"},
    )
    assert withdrawn.status_code == 201

    row = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == grant_id)
    )
    await db_session.refresh(row)
    assert row.revoked_at is not None

    blocked = await api_client.post(
        grants_url,
        headers=auth_headers,
        json={
            "caregiverId": str(caregiver.id),
            "familyAuthorization": {
                "authorizedAt": datetime.now(UTC).isoformat(),
                "reviewed": True,
            },
        },
    )
    assert blocked.status_code == 409

    audit = await api_client.get(
        f"/api/v1/patients/{patient.id}/access-events",
        headers=auth_headers,
        params={"page": 1, "limit": 100},
    )
    assert audit.status_code == 200
    actions = {item["action"] for item in audit.json()["items"]}
    assert "home_program_grant_revoked" in actions
