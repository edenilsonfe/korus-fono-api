"""F16 — grants do programa de casa: token rotativo, validade e revogações.

Cobre: token opaco devolvido uma única vez (hash persistido), rotação com um
único grant ativo por programa, limites 1–30 dias (padrão 14) com teto de fim do
programa + 7 dias, gate ABA com consentimento gravado, retirada que invalida a
derivação sem ressuscitar link antigo, revogação idempotente/read-only e escopo
do dono. SQLite em memória.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.care_team import PatientCareTeamMember
from app.models.caregiver import Caregiver
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgram,
    HomeProgramGrant,
    HomeProgramTask,
)
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.professional import Professional
from app.utils.token_hash import hash_token

TODAY = date.today()


def _headers(professional: Professional) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_access_token(professional.id, professional.token_version)}"
        )
    }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _professional(
    db_session, *, email: str, name: str = "Dra. Apoio"
) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=name,
        specialty_key="fono",
        specialty="Fonoaudiologia",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


async def _enable_aba_flag(db_session) -> None:
    existing = await db_session.get(FeatureFlag, "multidisciplinary_aba")
    if existing is None:
        db_session.add(
            FeatureFlag(
                key="multidisciplinary_aba",
                description="Equipe multiprofissional e programas ABA",
                enabled_global=True,
            )
        )
    else:
        existing.enabled_global = True
    await db_session.commit()


async def _consent(api_client, auth_headers, patient: Patient, decision: str = "granted") -> dict:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": decision, "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _goal(db_session, patient: Patient, professional: Professional) -> Goal:
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Nomear animais",
        area="Linguagem",
        start_date=TODAY,
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _aba_program(
    db_session, patient: Patient, professional: Professional
) -> InterventionProgram:
    program = InterventionProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Ensino de tato",
        operational_definition="Nomeia figuras de animais.",
        teaching_strategy="Tentativas discretas.",
        mastery_percent=80,
        mastery_consecutive_sessions=3,
        status="active",
    )
    db_session.add(program)
    await db_session.commit()
    await db_session.refresh(program)
    return program


def _task(*, goal_id: UUID | None = None, aba_id: UUID | None = None, due_on: date | None = None) -> dict:
    return {
        "title": "Nomear figuras",
        "instructions": "Mostre os cartões.",
        "dueOn": (due_on or TODAY).isoformat(),
        "goalId": str(goal_id) if goal_id else None,
        "interventionProgramId": str(aba_id) if aba_id else None,
        "resourceIds": [],
    }


async def _published_program(
    api_client,
    auth_headers,
    patient: Patient,
    db_session,
    professional: Professional,
    *,
    aba: bool = False,
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> dict:
    goal = None
    aba_id = None
    if aba:
        aba_id = (await _aba_program(db_session, patient, professional)).id
    else:
        goal = await _goal(db_session, patient, professional)
    body = {
        "title": "Rotina de casa",
        "startsOn": (starts_on or TODAY).isoformat(),
        "endsOn": (ends_on or TODAY + timedelta(days=13)).isoformat(),
        "tasks": [
            _task(goal_id=goal.id if goal else None, aba_id=aba_id, due_on=starts_on or TODAY)
        ],
    }
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=body,
    )
    assert created.status_code == 201, created.text
    data = created.json()
    published = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{data['id']}/publish",
        headers=auth_headers,
        json={"expectedVersion": 1},
    )
    assert published.status_code == 200, published.text
    return published.json()


def _grant_body(caregiver_id, *, days: int | None = None, authorized_at: datetime | None = None, **overrides) -> dict:
    body = {
        "caregiverId": str(caregiver_id),
        "familyAuthorization": {
            "authorizedAt": (
                authorized_at or datetime.now(UTC) - timedelta(days=1)
            ).isoformat(),
            "reference": "Termo de autorização arquivado",
            "reviewed": True,
        },
    }
    if days is not None:
        body["expiresInDays"] = days
    body.update(overrides)
    return body


async def _primary_caregiver(db_session, patient: Patient) -> Caregiver:
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    return caregiver


# ---------------------------------------------------------------------------
# Emissão e listagem
# ---------------------------------------------------------------------------


async def test_grant_requires_active_program_and_owner(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json={
            "title": "Rascunho",
            "startsOn": TODAY.isoformat(),
            "endsOn": (TODAY + timedelta(days=5)).isoformat(),
            "tasks": [_task(goal_id=goal.id)],
        },
    )
    assert created.status_code == 201
    program_id = created.json()["id"]
    caregiver = await _primary_caregiver(db_session, patient)

    draft = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{program_id}/grants",
        headers=auth_headers,
        json=_grant_body(caregiver.id),
    )
    assert draft.status_code == 409
    assert "Publique" in draft.json()["detail"]

    other = await _professional(db_session, email="fora@example.com")
    denied = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{program_id}/grants",
        headers=_headers(other),
        json=_grant_body(caregiver.id),
    )
    assert denied.status_code == 404


async def test_grant_token_appears_once_and_hash_is_persisted(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"

    created = await api_client.post(
        url, headers=auth_headers, json=_grant_body(caregiver.id, days=7)
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["caregiverId"] == str(caregiver.id)
    assert data["recipientLabel"] == "Maria Silva (Mãe)"
    assert data["revokedAt"] is None
    assert "#token=" in data["url"]
    raw_token = data["url"].split("#token=", 1)[1]
    assert len(raw_token) >= 32
    assert data["url"].endswith(f"/programa-de-casa#token={raw_token}")
    expires = _as_utc(datetime.fromisoformat(data["expiresAt"]))
    assert abs((expires - (datetime.now(UTC) + timedelta(days=7))).total_seconds()) < 300

    row = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == UUID(data["id"]))
    )
    await db_session.refresh(row)
    assert row.token_hash == hash_token(raw_token)
    assert row.token_hash != raw_token
    assert row.caregiver_name_snapshot == "Maria Silva"
    assert row.caregiver_relation_snapshot == "Mãe"
    assert row.family_authorization is not None
    assert row.family_authorization["reference"] == "Termo de autorização arquivado"
    assert row.consent_event_id is None  # programa comum não exige consentimento

    # GET devolve metadados sem url/token
    listing = await api_client.get(url, headers=auth_headers)
    assert listing.status_code == 200, listing.text
    items = listing.json()
    assert len(items) == 1
    assert items[0]["id"] == data["id"]
    assert items[0]["url"] is None
    assert raw_token not in listing.text


async def test_grant_expiry_default_bounds_and_program_cap(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"

    # padrão: 14 dias
    default = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert default.status_code == 201, default.text
    expires = _as_utc(datetime.fromisoformat(default.json()["expiresAt"]))
    assert abs((expires - (datetime.now(UTC) + timedelta(days=14))).total_seconds()) < 300

    # fora de 1..30
    for invalid in (0, 31):
        response = await api_client.post(
            url, headers=auth_headers, json=_grant_body(caregiver.id, days=invalid)
        )
        assert response.status_code == 422, (invalid, response.text)

    # teto: fim do programa + 7 dias
    short = await _published_program(
        api_client,
        auth_headers,
        patient,
        db_session,
        professional,
        starts_on=TODAY,
        ends_on=TODAY + timedelta(days=10),
    )
    short_url = f"/api/v1/patients/{patient.id}/home-programs/{short['id']}/grants"
    capped = await api_client.post(
        short_url, headers=auth_headers, json=_grant_body(caregiver.id, days=30)
    )
    assert capped.status_code == 201, capped.text
    expires = _as_utc(datetime.fromisoformat(capped.json()["expiresAt"]))
    cap_day = TODAY + timedelta(days=17)
    assert expires.date() == cap_day
    assert expires.hour == 23 and expires.minute == 59

    # programa cujo período já encerrou não gera link
    expired = await _published_program(
        api_client,
        auth_headers,
        patient,
        db_session,
        professional,
        starts_on=TODAY - timedelta(days=20),
        ends_on=TODAY - timedelta(days=10),
    )
    expired_url = f"/api/v1/patients/{patient.id}/home-programs/{expired['id']}/grants"
    refused = await api_client.post(
        expired_url, headers=auth_headers, json=_grant_body(caregiver.id)
    )
    assert refused.status_code == 409
    assert "encerrou" in refused.json()["detail"]


async def test_grant_validation_and_caregiver_scope(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"

    other = await _professional(db_session, email="dona-outra@example.com")
    foreign_patient = Patient(
        professional_id=other.id,
        name="Paciente alheio",
        birth_date=date(2019, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign_patient)
    await db_session.flush()
    foreign_caregiver = Caregiver(
        patient_id=foreign_patient.id, name="Responsável alheio", relation="Mãe"
    )
    db_session.add(foreign_caregiver)
    await db_session.commit()
    await db_session.refresh(foreign_caregiver)

    alien = await api_client.post(
        url, headers=auth_headers, json=_grant_body(foreign_caregiver.id)
    )
    assert alien.status_code == 404

    # autorização da família obrigatória e coerente
    missing = await api_client.post(
        url,
        headers=auth_headers,
        json={"caregiverId": str(caregiver.id)},
    )
    assert missing.status_code == 422

    unreviewed = await api_client.post(
        url,
        headers=auth_headers,
        json=_grant_body(caregiver.id, familyAuthorization={
            "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "reviewed": False,
        }),
    )
    assert unreviewed.status_code == 422

    future = await api_client.post(
        url,
        headers=auth_headers,
        json=_grant_body(caregiver.id, authorized_at=datetime.now(UTC) + timedelta(days=2)),
    )
    assert future.status_code == 422

    unknown = await api_client.post(
        url,
        headers=auth_headers,
        json=_grant_body(caregiver.id, extraField=True),
    )
    assert unknown.status_code == 422


# ---------------------------------------------------------------------------
# Rotação e revogação
# ---------------------------------------------------------------------------


async def test_grant_rotation_keeps_single_active(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"

    first = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert first.status_code == 201, first.text
    second = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert second.status_code == 201, second.text
    first_token = first.json()["url"].split("#token=", 1)[1]
    second_token = second.json()["url"].split("#token=", 1)[1]
    assert first_token != second_token

    rows = (
        (
            await db_session.execute(
                select(HomeProgramGrant).where(
                    HomeProgramGrant.program_id == UUID(program["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    active = [row for row in rows if row.revoked_at is None]
    assert [row.id for row in active] == [UUID(second.json()["id"])]
    rotated = next(row for row in rows if row.id == UUID(first.json()["id"]))
    assert rotated.revoked_at is not None
    assert rotated.revoked_by_professional_id == professional.id

    # um novo grant continua permitido depois da rotação e da revogação
    revoked = await api_client.delete(
        url + f"/{second.json()['id']}", headers=auth_headers
    )
    assert revoked.status_code == 204
    third = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert third.status_code == 201, third.text


async def test_revoke_grant_is_idempotent_and_owner_scoped(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    caregiver = await _primary_caregiver(db_session, patient)
    base = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"
    created = await api_client.post(base, headers=auth_headers, json=_grant_body(caregiver.id))
    assert created.status_code == 201
    grant_id = created.json()["id"]

    # concluir o get inicial da listagem antes do DELETE (metadados intactos)
    listing = await api_client.get(base, headers=auth_headers)
    assert listing.json()[0]["revokedAt"] is None

    first = await api_client.delete(base + f"/{grant_id}", headers=auth_headers)
    assert first.status_code == 204
    row = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == UUID(grant_id))
    )
    await db_session.refresh(row)
    assert row.revoked_at is not None
    revoked_at = row.revoked_at

    # repetir continua 204 e não reescreve a revogação
    again = await api_client.delete(base + f"/{grant_id}", headers=auth_headers)
    assert again.status_code == 204
    await db_session.refresh(row)
    assert row.revoked_at == revoked_at

    missing = await api_client.delete(base + f"/{uuid4()}", headers=auth_headers)
    assert missing.status_code == 404

    other = await _professional(db_session, email="intrusa@example.com")
    denied = await api_client.delete(base + f"/{grant_id}", headers=_headers(other))
    assert denied.status_code == 404

    # membro da equipe lê acompanhamento, nunca grants/contatos
    await _enable_aba_flag(db_session)
    consent = await _consent(api_client, auth_headers, patient)
    invited = await _professional(db_session, email="equipe@example.com")
    db_session.add(
        PatientCareTeamMember(
            patient_id=patient.id,
            professional_id=invited.id,
            role="practitioner",
            status="active",
            invited_by_professional_id=professional.id,
            consent_event_id=UUID(consent["id"]),
            invited_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    shared_grants = await api_client.get(base, headers=_headers(invited))
    assert shared_grants.status_code == 404


# ---------------------------------------------------------------------------
# Gate ABA: consentimento e retirada
# ---------------------------------------------------------------------------


async def test_aba_gate_withdrawal_invalidates_derivation(
    api_client, auth_headers, db_session, patient, professional
):
    await _enable_aba_flag(db_session)
    consent = await _consent(api_client, auth_headers, patient)
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, aba=True
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants"

    created = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert created.status_code == 201, created.text
    grant_id = UUID(created.json()["id"])
    row = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == grant_id)
    )
    await db_session.refresh(row)
    assert row.consent_event_id == UUID(consent["id"])

    # retirada do consentimento invalida a derivação: grant ativo é revogado
    await _consent(api_client, auth_headers, patient, decision="withdrawn")
    await db_session.refresh(row)
    assert row.revoked_at is not None

    # concessão posterior bloqueada enquanto a autorização estiver retirada
    blocked = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert blocked.status_code == 409

    # novo consentimento permite uma nova concessão; o link antigo segue morto
    new_consent = await _consent(api_client, auth_headers, patient)
    renewed = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert renewed.status_code == 201, renewed.text
    assert renewed.json()["id"] != str(grant_id)
    await db_session.refresh(row)
    assert row.revoked_at is not None  # não ressuscita
    fresh = await db_session.scalar(
        select(HomeProgramGrant).where(
            HomeProgramGrant.id == UUID(renewed.json()["id"])
        )
    )
    await db_session.refresh(fresh)
    assert fresh.revoked_at is None
    assert fresh.consent_event_id == UUID(new_consent["id"])
    assert fresh.consent_event_id != row.consent_event_id


async def test_common_program_grants_survive_consent_withdrawal(
    api_client, auth_headers, db_session, patient, professional
):
    """Meta comum não depende de gate ABA: a retirada não revoga seu link."""
    await _enable_aba_flag(db_session)
    common = await _published_program(
        api_client, auth_headers, patient, db_session, professional, aba=False
    )
    caregiver = await _primary_caregiver(db_session, patient)
    url = f"/api/v1/patients/{patient.id}/home-programs/{common['id']}/grants"
    created = await api_client.post(url, headers=auth_headers, json=_grant_body(caregiver.id))
    assert created.status_code == 201, created.text

    await _consent(api_client, auth_headers, patient, decision="withdrawn")

    row = await db_session.scalar(
        select(HomeProgramGrant).where(
            HomeProgramGrant.id == UUID(created.json()["id"])
        )
    )
    await db_session.refresh(row)
    assert row.revoked_at is None
    assert row.consent_event_id is None


# ---------------------------------------------------------------------------
# Read-only (entitlement): revogar/arquivar são ações protetivas
# ---------------------------------------------------------------------------


async def test_read_only_allows_revoke_and_archive_but_not_writes(
    api_client, db_session, patient, professional
):
    readonly = await _professional(db_session, email="vencida@example.com", name="Plano vencido")
    readonly.subscription_status = "canceled"
    await db_session.commit()
    headers = _headers(readonly)

    own_patient = Patient(
        professional_id=readonly.id,
        name="Paciente em read-only",
        birth_date=date(2018, 6, 6),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(own_patient)
    await db_session.flush()
    caregiver = Caregiver(
        patient_id=own_patient.id, name="Responsável ativo", relation="Mãe"
    )
    db_session.add(caregiver)
    goal = Goal(
        patient_id=own_patient.id,
        professional_id=readonly.id,
        title="Meta ativa",
        area="Linguagem",
        start_date=TODAY,
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.flush()
    program = HomeProgram(
        patient_id=own_patient.id,
        created_by_professional_id=readonly.id,
        title="Programa ativo",
        status="active",
        version=2,
        starts_on=TODAY,
        ends_on=TODAY + timedelta(days=10),
        timezone="America/Sao_Paulo",
    )
    db_session.add(program)
    await db_session.flush()
    db_session.add(
        HomeProgramTask(
            program_id=program.id,
            position=0,
            client_task_id=uuid4(),
            title="Tarefa ativa",
            instructions="Instruções",
            due_on=TODAY,
            goal_id=goal.id,
        )
    )
    grant = HomeProgramGrant(
        program_id=program.id,
        caregiver_id=caregiver.id,
        caregiver_name_snapshot="Responsável ativo",
        caregiver_relation_snapshot="Mãe",
        token_hash=hash_token("token-readonly"),
        expires_at=datetime.now(UTC) + timedelta(days=5),
        created_by_professional_id=readonly.id,
        family_authorization={"authorizedAt": datetime.now(UTC).isoformat(), "reviewed": True},
    )
    db_session.add(grant)
    await db_session.commit()
    await db_session.refresh(program)
    await db_session.refresh(grant)

    base = f"/api/v1/patients/{own_patient.id}/home-programs"

    # leituras seguem disponíveis
    assert (await api_client.get(base, headers=headers)).status_code == 200
    assert (await api_client.get(base + f"/{program.id}/grants", headers=headers)).status_code == 200

    # mutações comuns bloqueadas pelo entitlement
    create = await api_client.post(
        base,
        headers=headers,
        json={
            "title": "Bloqueado",
            "startsOn": TODAY.isoformat(),
            "endsOn": (TODAY + timedelta(days=5)).isoformat(),
            "tasks": [_task(goal_id=goal.id)],
        },
    )
    assert create.status_code == 403
    assert create.json().get("type") == "entitlement_error"

    publish = await api_client.post(
        base + f"/{program.id}/publish", headers=headers, json={"expectedVersion": 2}
    )
    assert publish.status_code == 403

    # revogar o link e arquivar o programa são ações protetivas (exceção exata)
    revoked = await api_client.delete(
        base + f"/{program.id}/grants/{grant.id}", headers=headers
    )
    assert revoked.status_code == 204, revoked.text
    await db_session.refresh(grant)
    assert grant.revoked_at is not None

    archived = await api_client.post(base + f"/{program.id}/archive", headers=headers, json={})
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    await db_session.refresh(program)
    assert program.status == "archived"
