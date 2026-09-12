"""F14 onda 1 — acesso privado do portal da família (autorizações e grants).

Cobre §3.1–3.2: portal habilitável/desabilitável com versões otimistas,
autorização append-only por responsável, permissão de agenda, retirada
idempotente, emissão/rotação/revogação de links com token só em fragmento,
trilha de eventos e raiz pública mínima §3.4. SQLite em memória; as corridas
reais ficam no gate PostgreSQL (``test_family_portal_concurrency.py``).
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_password
from app.models.caregiver import Caregiver
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalEvent,
    FamilyPortalGrant,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.utils.token_hash import hash_token

TODAY = date.today()
TOKEN_HEADER = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: contador público sempre 'permite'."""
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


def _headers(professional: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


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


async def _caregiver(
    db_session,
    patient: Patient,
    *,
    name: str = "Responsável Teste",
    relation: str = "Mãe",
    is_primary: bool = False,
) -> Caregiver:
    caregiver = Caregiver(
        patient_id=patient.id, name=name, relation=relation, is_primary=is_primary
    )
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


def _base(patient: Patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _enable(api_client, headers, patient: Patient) -> dict:
    response = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _authorization_body(
    *,
    authorized_at: datetime | None = None,
    reference: str | None = "Termo de autorização arquivado",
    appointments: bool = False,
    expected_version: int | None = None,
) -> dict:
    body: dict = {
        "appointmentsEnabled": appointments,
        "familyAuthorization": {
            "authorizedAt": (
                authorized_at or datetime.now(UTC) - timedelta(days=1)
            ).isoformat(),
            "reference": reference,
            "reviewed": True,
        },
    }
    if expected_version is not None:
        body["expectedVersion"] = expected_version
    return body


async def _authorize(
    api_client, headers, patient: Patient, caregiver_id, **kwargs
) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json=_authorization_body(**kwargs),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue_grant(
    api_client,
    headers,
    patient: Patient,
    recipient_id,
    *,
    expected_recipient_version: int = 1,
    rotate_from_grant_id=None,
    expires_in_days: int = 30,
):
    body: dict = {
        "expiresInDays": expires_in_days,
        "expectedRecipientVersion": expected_recipient_version,
    }
    if rotate_from_grant_id is not None:
        body["rotateFromGrantId"] = str(rotate_from_grant_id)
    return await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/grants",
        headers=headers,
        json=body,
    )


def _raw_token(response) -> str:
    return response.json()["url"].split("#token=", 1)[1]


async def _primary_caregiver(db_session, patient: Patient) -> Caregiver:
    return await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )


async def _portal_rows(db_session) -> list[FamilyPortal]:
    return (
        (await db_session.execute(select(FamilyPortal))).scalars().all()
    )


async def _event_types(db_session) -> list[str]:
    return [
        row.event_type
        for row in (
            (
                await db_session.execute(
                    select(FamilyPortalEvent).order_by(FamilyPortalEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        )
    ]


# --------------------------------------------------------------------------- #
# Portal: leitura, habilitação e desativação
# --------------------------------------------------------------------------- #


async def test_portal_starts_disabled_and_read_creates_nothing(
    api_client, auth_headers, db_session, patient
):
    response = await api_client.get(_base(patient), headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": None,
        "patientId": str(patient.id),
        "enabled": False,
        "version": 1,
        "recipientCount": 0,
        "publishedItemCount": 0,
    }
    assert await _portal_rows(db_session) == []
    assert await db_session.scalar(
        select(func.count()).select_from(FamilyPortalEvent)
    ) == 0


async def test_enable_portal_is_idempotent_and_versioned(
    api_client, auth_headers, db_session, patient
):
    base = _base(patient)
    created = await api_client.put(
        base, headers=auth_headers, json={"enabled": True, "expectedVersion": 1}
    )
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["enabled"] is True and data["version"] == 1
    assert data["id"] is not None

    repeat = await api_client.put(
        base, headers=auth_headers, json={"enabled": True, "expectedVersion": 1}
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["id"] == data["id"]

    rows = await _portal_rows(db_session)
    assert len(rows) == 1
    assert await db_session.scalar(
        select(func.count())
        .select_from(FamilyPortalEvent)
        .where(FamilyPortalEvent.event_type == "portal_enabled")
    ) == 1

    stale = await api_client.put(
        base, headers=auth_headers, json={"enabled": True, "expectedVersion": 2}
    )
    assert stale.status_code == 409

    # Desabilitar NÃO é aceito aqui: ação protetiva é /disable.
    invalid = await api_client.put(
        base, headers=auth_headers, json={"enabled": False, "expectedVersion": 1}
    )
    assert invalid.status_code == 422

    unknown = await api_client.put(
        base,
        headers=auth_headers,
        json={"enabled": True, "expectedVersion": 1, "extra": "x"},
    )
    assert unknown.status_code == 422


async def test_enable_portal_refuses_inactive_patient(
    api_client, auth_headers, db_session, patient
):
    patient.status = "inativo"
    await db_session.commit()
    response = await api_client.put(
        _base(patient),
        headers=auth_headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 409
    assert "inativo" in response.json()["detail"].lower()


async def test_disable_portal_revokes_grants_and_kills_tokens_forever(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    _ = await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    issued = await _issue_grant(api_client, headers, patient, recipient["id"])
    assert issued.status_code == 201, issued.text
    token = _raw_token(issued)

    assert (await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})).status_code == 200

    disabled = await api_client.post(
        f"{_base(patient)}/disable", headers=headers, json={}
    )
    assert disabled.status_code == 200, disabled.text
    data = disabled.json()
    assert data["enabled"] is False and data["version"] == 2

    portal = (await _portal_rows(db_session))[0]
    await db_session.refresh(portal)
    assert portal.access_version == 1

    grant = await db_session.scalar(
        select(FamilyPortalGrant).where(
            FamilyPortalGrant.id == UUID(issued.json()["id"])
        )
    )
    await db_session.refresh(grant)
    assert grant.revoked_at is not None
    assert (await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})).status_code == 410

    # Repetir é inócuo: nem nova versão nem novo evento.
    again = await api_client.post(
        f"{_base(patient)}/disable", headers=headers, json={}
    )
    assert again.status_code == 200
    assert again.json()["version"] == 2
    await db_session.refresh(portal)
    assert portal.access_version == 1
    assert (await _event_types(db_session)).count("portal_disabled") == 1

    # Reabilitar exige nova versão e NÃO ressuscita o link antigo.
    reenabled = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 2},
    )
    assert reenabled.status_code == 200, reenabled.text
    assert reenabled.json()["version"] == 3
    assert (await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})).status_code == 410


async def test_disable_without_portal_does_not_create_rows(
    api_client, auth_headers, db_session, patient
):
    response = await api_client.post(
        f"{_base(patient)}/disable", headers=auth_headers, json={}
    )
    assert response.status_code == 200
    assert response.json()["id"] is None
    assert response.json()["enabled"] is False
    assert await _portal_rows(db_session) == []


# --------------------------------------------------------------------------- #
# Destinatários: autorização versionada append-only
# --------------------------------------------------------------------------- #


async def test_recipient_authorization_is_append_only_and_versioned(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)

    first = await _authorize(api_client, headers, patient, caregiver.id)
    assert first["active"] is True
    assert first["version"] == 1 and first["authorizationVersion"] == 1
    assert first["caregiverId"] == str(caregiver.id)
    assert first["recipientLabel"] == "Maria Silva (Mãe)"
    assert first["authorizedAt"] is not None and first["recordedAt"] is not None
    assert first["currentGrant"] is None
    recipient_id = first["id"]

    events = await api_client.get(f"{_base(patient)}/events", headers=headers)
    assert events.status_code == 200, events.text
    payload = events.json()
    assert payload["total"] == 2  # portal_enabled + recipient_authorized
    event = next(
        item
        for item in payload["items"]
        if item["type"] == "recipient_authorized"
    )
    assert event["recipientId"] == recipient_id
    assert event["authorization"]["reviewed"] is True
    assert (
        event["authorization"]["reference"] == "Termo de autorização arquivado"
    )
    assert datetime.fromisoformat(
        event["authorization"]["authorizedAt"]
    ) == datetime.fromisoformat(first["authorizedAt"])
    assert event["itemId"] is None and event["grantId"] is None

    # Nova autorização (novo authorizedAt) exige a versão corrente e incrementa.
    second = await _authorize(
        api_client,
        headers,
        patient,
        caregiver.id,
        authorized_at=datetime.now(UTC) - timedelta(hours=2),
        expected_version=1,
    )
    assert second["version"] == 2 and second["authorizationVersion"] == 2

    # Replay do payload antigo com versão obsoleta → 409 (sem evento de replay).
    stale = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json=_authorization_body(
            authorized_at=datetime.fromisoformat(first["authorizedAt"]),
            expected_version=1,
        ),
    )
    assert stale.status_code == 409

    # Mesmo payload com a versão corrente → no-op, sem nova versão/evento.
    current_replay = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json=_authorization_body(
            authorized_at=datetime.fromisoformat(second["authorizedAt"]),
            expected_version=2,
        ),
    )
    assert current_replay.status_code == 200, current_replay.text
    assert current_replay.json()["version"] == 2
    events_after = (
        await api_client.get(f"{_base(patient)}/events", headers=headers)
    ).json()
    assert events_after["total"] == 3  # portal_enabled + 2 autorizações

    # Novo destinatário com expectedVersion preenchido → 409.
    other_caregiver = await _caregiver(
        db_session, patient, name="Outro Responsável", relation="Pai"
    )
    wrong_new = await api_client.put(
        f"{_base(patient)}/recipients/{other_caregiver.id}",
        headers=headers,
        json=_authorization_body(expected_version=1),
    )
    assert wrong_new.status_code == 409

    # Responsável de outro paciente → 404.
    foreign = await _professional(db_session, email="fora@example.com")
    foreign_patient = Patient(
        professional_id=foreign.id,
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
        patient_id=foreign_patient.id, name="Alheio", relation="Mãe"
    )
    db_session.add(foreign_caregiver)
    await db_session.commit()
    await db_session.refresh(foreign_caregiver)
    alien = await api_client.put(
        f"{_base(patient)}/recipients/{foreign_caregiver.id}",
        headers=headers,
        json=_authorization_body(),
    )
    assert alien.status_code == 404


async def test_recipient_authorization_requires_enabled_portal_and_valid_date(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    caregiver = await _primary_caregiver(db_session, patient)

    without_portal = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json=_authorization_body(),
    )
    assert without_portal.status_code == 409

    await _enable(api_client, headers, patient)

    naive = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json={
            "appointmentsEnabled": False,
            "familyAuthorization": {
                "authorizedAt": "2026-09-01T10:00:00",
                "reference": "Termo",
                "reviewed": True,
            },
        },
    )
    assert naive.status_code == 422
    assert "fuso" in naive.json()["detail"].lower()

    future = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json=_authorization_body(
            authorized_at=datetime.now(UTC) + timedelta(days=1)
        ),
    )
    assert future.status_code == 422
    assert "futuro" in future.json()["detail"].lower()

    unreviewed = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json={
            "appointmentsEnabled": False,
            "familyAuthorization": {
                "authorizedAt": (
                    datetime.now(UTC) - timedelta(days=1)
                ).isoformat(),
                "reviewed": False,
            },
        },
    )
    assert unreviewed.status_code == 422


async def test_recipient_appointments_toggle_and_delete_are_versioned(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    recipient_id = recipient["id"]
    base = f"{_base(patient)}/recipients/{recipient_id}"

    enabled = await api_client.patch(
        base,
        headers=headers,
        json={"expectedVersion": 1, "appointmentsEnabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["appointmentsEnabled"] is True
    assert enabled.json()["version"] == 2

    # Mesmo valor com a versão corrente → no-op (sem nova versão/evento).
    replay = await api_client.patch(
        base,
        headers=headers,
        json={"expectedVersion": 2, "appointmentsEnabled": True},
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 2

    stale = await api_client.patch(
        base,
        headers=headers,
        json={"expectedVersion": 1, "appointmentsEnabled": False},
    )
    assert stale.status_code == 409

    # DELETE é idempotente, permitido em read-only e não exige versão.
    removed = await api_client.delete(f"{base}/appointments", headers=headers)
    assert removed.status_code == 204, removed.text
    assert (await api_client.delete(f"{base}/appointments", headers=headers)).status_code == 204

    listing = await api_client.get(f"{_base(patient)}/recipients", headers=headers)
    assert listing.status_code == 200, listing.text
    item = listing.json()["items"][0]
    assert item["appointmentsEnabled"] is False
    assert item["version"] == 3


async def test_recipient_cap_of_ten_active_recipients(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    for index in range(10):
        caregiver = await _caregiver(
            db_session,
            patient,
            name=f"Responsável {index}",
            relation="Mãe",
            is_primary=False,
        )
        await _authorize(api_client, headers, patient, caregiver.id)
    overflow = await _caregiver(
        db_session, patient, name="Responsável 11", relation="Pai"
    )
    blocked = await api_client.put(
        f"{_base(patient)}/recipients/{overflow.id}",
        headers=headers,
        json=_authorization_body(),
    )
    assert blocked.status_code == 422
    assert "Limite" in blocked.json()["detail"]


# --------------------------------------------------------------------------- #
# Grants: emissão, rotação e revogação
# --------------------------------------------------------------------------- #


async def test_grant_issue_requires_enabled_portal_active_recipient_and_latest_version(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    caregiver = await _primary_caregiver(db_session, patient)
    without_portal = await _issue_grant(
        api_client, headers, patient, "00000000-0000-0000-0000-000000000000"
    )
    assert without_portal.status_code == 409

    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    stale_version = await _issue_grant(
        api_client,
        headers,
        patient,
        recipient["id"],
        expected_recipient_version=2,
    )
    assert stale_version.status_code == 409

    invalid_days = await _issue_grant(
        api_client, headers, patient, recipient["id"], expires_in_days=31
    )
    assert invalid_days.status_code == 422

    missing_version = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=headers,
        json={"expiresInDays": 30},
    )
    assert missing_version.status_code == 422

    # Retirado não emite link.
    await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/withdraw",
        headers=headers,
        json={"reason": "professional_decision"},
    )
    withdrawn = await _issue_grant(api_client, headers, patient, recipient["id"])
    assert withdrawn.status_code == 409


async def test_grant_token_appears_once_hashed_and_rotation_is_transactional(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    recipient_id = recipient["id"]
    base = f"{_base(patient)}/recipients/{recipient_id}/grants"

    created = await _issue_grant(
        api_client, headers, patient, recipient_id, expires_in_days=5
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["status"] == "active" and data["revokedAt"] is None
    assert data["recipientId"] == recipient_id
    assert data["url"].endswith(f"/responsaveis#token={_raw_token(created)}")
    raw_token = _raw_token(created)
    assert len(raw_token) >= 32
    expires = _as_utc(datetime.fromisoformat(data["expiresAt"]))
    assert abs((expires - (datetime.now(UTC) + timedelta(days=5))).total_seconds()) < 300

    row = await db_session.scalar(
        select(FamilyPortalGrant).where(FamilyPortalGrant.id == UUID(data["id"]))
    )
    await db_session.refresh(row)
    assert row.token_hash == hash_token(raw_token)
    assert row.token_hash != raw_token
    assert row.owner_access_version == 0
    assert row.portal_access_version == 0

    # Metadados nunca devolvem url/token/hash.
    listing = await api_client.get(base, headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    assert "url" not in body["items"][0]
    assert raw_token not in listing.text
    assert "tokenHash" not in listing.text

    # Segunda emissão sem rotateFromGrantId → 409 (nunca sucesso perdido).
    double = await _issue_grant(api_client, headers, patient, recipient_id)
    assert double.status_code == 409

    # Rotação informa o grant anterior (mesmo expirado/validado).
    rotated = await _issue_grant(
        api_client,
        headers,
        patient,
        recipient_id,
        rotate_from_grant_id=UUID(data["id"]),
    )
    assert rotated.status_code == 201, rotated.text
    assert rotated.json()["id"] != data["id"]
    assert _raw_token(rotated) != raw_token

    rows = (
        (
            await db_session.execute(
                select(FamilyPortalGrant).where(
                    FamilyPortalGrant.recipient_id == UUID(recipient_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    active = [row for row in rows if row.revoked_at is None]
    assert [row.id for row in active] == [UUID(rotated.json()["id"])]
    rotated_previous = next(row for row in rows if row.id == UUID(data["id"]))
    assert rotated_previous.revoked_at is not None
    assert rotated_previous.revoked_by_professional_id is not None

    # O token anterior morre imediatamente; o novo funciona.
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: raw_token})
    ).status_code == 410
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: _raw_token(rotated)})
    ).status_code == 200

    # Revogação idempotente no escopo; sem link ativo, rotateFromGrantId deve ser nulo.
    first_revoke = await api_client.delete(
        f"{base}/{rotated.json()['id']}", headers=headers
    )
    assert first_revoke.status_code == 204
    assert (
        await api_client.delete(f"{base}/{rotated.json()['id']}", headers=headers)
    ).status_code == 204
    missing = await api_client.delete(
        f"{base}/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert missing.status_code == 404

    rotated_again = await _issue_grant(
        api_client,
        headers,
        patient,
        recipient_id,
        rotate_from_grant_id=UUID(rotated.json()["id"]),
    )
    assert rotated_again.status_code == 409
    fresh = await _issue_grant(api_client, headers, patient, recipient_id)
    assert fresh.status_code == 201, fresh.text

    # Status invalidated quando a época do portal divergir.
    portal = (await _portal_rows(db_session))[0]
    portal.access_version += 1
    await db_session.commit()
    statuses = (
        await api_client.get(base, headers=headers)
    ).json()["items"]
    by_id = {item["id"]: item["status"] for item in statuses}
    assert by_id[fresh.json()["id"]] == "invalidated"
    assert by_id[rotated.json()["id"]] == "revoked"


async def test_authorization_change_revokes_previous_grant(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    issued = await _issue_grant(api_client, headers, patient, recipient["id"])
    assert issued.status_code == 201
    token = _raw_token(issued)

    updated = await _authorize(
        api_client,
        headers,
        patient,
        caregiver.id,
        authorized_at=datetime.now(UTC) - timedelta(days=3),
        expected_version=1,
    )
    assert updated["authorizationVersion"] == 2

    listing = await api_client.get(
        f"{_base(patient)}/recipients/{recipient['id']}/grants", headers=headers
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"][0]["status"] == "revoked"
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 410

    # Emitir outro link é ação explícita (sem rotação pendente).
    again = await _issue_grant(
        api_client,
        headers,
        patient,
        recipient["id"],
        expected_recipient_version=updated["version"],
    )
    assert again.status_code == 201, again.text


async def test_withdraw_recipient_revokes_and_reactivation_does_not_restore(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    issued = await _issue_grant(api_client, headers, patient, recipient["id"])
    assert issued.status_code == 201
    token = _raw_token(issued)
    recipient_id = recipient["id"]

    withdrawn = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/withdraw",
        headers=headers,
        json={"reason": "incorrect_recipient"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    data = withdrawn.json()
    assert data["active"] is False
    assert data["version"] == 2 and data["authorizationVersion"] == 2
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 410

    # Repetir não duplica evento nem incrementa versão.
    repeated = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/withdraw",
        headers=headers,
        json={"reason": "professional_decision"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == 2
    assert (await _event_types(db_session)).count("recipient_withdrawn") == 1

    invalid_reason = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/withdraw",
        headers=headers,
        json={"reason": "outro"},
    )
    assert invalid_reason.status_code == 422

    # Nova autorização reativa, mas NÃO ressuscita o link nem o público antigo.
    reactivated = await _authorize(
        api_client,
        headers,
        patient,
        caregiver.id,
        authorized_at=datetime.now(UTC) - timedelta(days=1),
        expected_version=2,
    )
    assert reactivated["active"] is True
    assert reactivated["authorizationVersion"] == 3
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 410
    grants = await api_client.get(
        f"{_base(patient)}/recipients/{recipient_id}/grants", headers=headers
    )
    assert grants.json()["items"][0]["status"] == "revoked"
    fresh = await _issue_grant(
        api_client,
        headers,
        patient,
        recipient_id,
        expected_recipient_version=reactivated["version"],
    )
    assert fresh.status_code == 201, fresh.text
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: _raw_token(fresh)})
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Raiz pública §3.4
# --------------------------------------------------------------------------- #


async def test_public_root_exposes_minimal_allowlist(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(
        api_client, headers, patient, caregiver.id, appointments=True
    )
    issued = await _issue_grant(api_client, headers, patient, recipient["id"])
    assert issued.status_code == 201, issued.text
    token = _raw_token(issued)

    response = await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data.keys()) == {
        "patientFirstName",
        "professional",
        "expiresAt",
        "timezone",
        "appointmentsEnabled",
        "sections",
    }
    assert data["patientFirstName"] == "João"
    assert data["professional"] == {"name": "Dra. Teste", "council": "CREFITO"}
    assert data["appointmentsEnabled"] is True
    assert data["timezone"] == "America/Sao_Paulo"
    assert data["sections"] == [
        "session_summary",
        "goal",
        "material",
        "report",
        "notice",
    ]
    expires = _as_utc(datetime.fromisoformat(data["expiresAt"]))
    assert abs((expires - _as_utc(datetime.fromisoformat(issued.json()["expiresAt"]))).total_seconds()) < 1

    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"

    for forbidden in ("patientId", "caregiverId", "grantId", "token", "Silva"):
        assert forbidden not in response.text

    # GET não cria linha/evento/recibo algum.
    assert await db_session.scalar(
        select(func.count()).select_from(FamilyPortalGrant)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(FamilyPortalEvent)
    ) == 3  # portal_enabled + recipient_authorized + grant_issued


async def test_recipient_listing_uses_neutral_label_after_caregiver_removed(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)

    # Hook de retirada derivada (o caller legado conecta na 1.2): o serviço
    # apenas faz flush; aqui simulamos a exclusão com o mesmo efeito.
    from app.services import family_portal_access

    await family_portal_access.withdraw_recipient_for_caregiver(
        db_session,
        patient_id=patient.id,
        caregiver_id=caregiver.id,
        actor=professional,
        reason="caregiver_removed",
        clear_caregiver_link=True,
    )
    await db_session.commit()

    listing = await api_client.get(f"{_base(patient)}/recipients", headers=headers)
    assert listing.status_code == 200, listing.text
    item = listing.json()["items"][0]
    assert item["id"] == recipient["id"]
    assert item["active"] is False
    assert item["caregiverId"] is None
    assert item["recipientLabel"] == "Responsável"


async def test_events_pagination_and_scope(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    await _authorize(api_client, headers, patient, caregiver.id)

    page = await api_client.get(
        f"{_base(patient)}/events?page=1&limit=1", headers=headers
    )
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["page"] == 1 and body["limit"] == 1
    assert body["total"] == 2 and len(body["items"]) == 1

    invalid = await api_client.get(
        f"{_base(patient)}/events?page=0", headers=headers
    )
    assert invalid.status_code == 422
    overflow = await api_client.get(
        f"{_base(patient)}/recipients?limit=51", headers=headers
    )
    assert overflow.status_code == 422
    bad_uuid = await api_client.get(
        "/api/v1/patients/nao-e-uuid/family-portal", headers=headers
    )
    assert bad_uuid.status_code == 422


async def test_portal_management_is_owner_scoped(
    api_client, db_session, patient, professional, auth_headers
):
    other = await _professional(db_session, email="intrusa@example.com")
    for method, path, body in (
        ("get", _base(patient), None),
        ("put", _base(patient), {"enabled": True, "expectedVersion": 1}),
        ("post", f"{_base(patient)}/disable", {}),
        ("get", f"{_base(patient)}/recipients", None),
        ("get", f"{_base(patient)}/events", None),
    ):
        kwargs = {"headers": _headers(other)}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(api_client, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.text)

    _ = await _enable(api_client, auth_headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, auth_headers, patient, caregiver.id)
    denied = await api_client.get(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=_headers(other),
    )
    assert denied.status_code == 404
