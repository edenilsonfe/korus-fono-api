"""F14 onda 1 — fronteira de segurança do portal da família.

Prova que: TODOS os estados inválidos do link viram um único 410 genérico
(sem enumerar paciente/motivo), o token é exclusivo do namespace F14 (não
intercambia com F16, Bearer ou query string), a rota pública é somente leitura
(qualquer outro método vira 405) e nunca devolve IDs internos, contatos,
diagnóstico ou o próprio token. SQLite em memória; PG fica no gate próprio.

Nota de onda: as exceções de entitlement read-only para as ações protetivas
(``POST /disable``, withdraw, DELETE de agenda/grants) chegam na tarefa 1.2 —
este arquivo NÃO fixa o comportamento pré-1.2 dessas mutações de propósito.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.auth_cookies import ACCESS_COOKIE
from app.core.security import create_access_token, hash_password
from app.models.caregiver import Caregiver
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalEvent,
    FamilyPortalGrant,
    FamilyPortalRecipient,
)
from app.models.home_program import HomeProgram, HomeProgramGrant
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.family_portal_access import FAMILY_PORTAL_LINK_GONE_MESSAGE
from app.utils.token_hash import hash_token

TODAY = date.today()
TOKEN_HEADER = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"
HOME_PROGRAM_PUBLIC = "/api/v1/home-program-responses"


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


def _headers(professional: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


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


def _base(patient: Patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


def _authorization_body(*, appointments: bool = False) -> dict:
    return {
        "appointmentsEnabled": appointments,
        "familyAuthorization": {
            "authorizedAt": (
                datetime.now(UTC) - timedelta(days=1)
            ).isoformat(),
            "reference": "Termo de autorização arquivado",
            "reviewed": True,
        },
    }


async def _provision(
    api_client, auth_headers, db_session, patient: Patient
) -> tuple[str, dict, dict]:
    """Portal habilitado + responsável principal autorizado + primeiro link."""
    await api_client.put(
        _base(patient),
        headers=auth_headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    authorized = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=auth_headers,
        json=_authorization_body(),
    )
    assert authorized.status_code == 200, authorized.text
    issued = await api_client.post(
        f"{_base(patient)}/recipients/{authorized.json()['id']}/grants",
        headers=auth_headers,
        json={"expiresInDays": 30, "expectedRecipientVersion": 1},
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["url"].split("#token=", 1)[1]
    return token, authorized.json(), issued.json()


async def _assert_gone(response) -> None:
    assert response.status_code == 410, response.text
    assert response.json() == {"detail": FAMILY_PORTAL_LINK_GONE_MESSAGE}
    # O corpo genérico não enumera paciente nem motivo.
    assert "Silva" not in response.text
    assert "grant" not in response.text.lower()


# --------------------------------------------------------------------------- #
# 410 único para TODOS os estados inválidos
# --------------------------------------------------------------------------- #


async def test_missing_and_malformed_tokens_are_the_same_generic_410(
    api_client, auth_headers, db_session, patient
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    await _assert_gone(await api_client.get(PUBLIC))
    await _assert_gone(await api_client.get(PUBLIC, headers={TOKEN_HEADER: ""}))
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: "desconhecido"})
    )
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: "x" * 129})
    )
    # Query string NUNCA substitui o header (sem fallback).
    await _assert_gone(await api_client.get(f"{PUBLIC}?token={token}"))
    # Token válido de OUTRO namespace (F16/F1) não vale aqui.
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: f"f16-{token}"})
    )


async def test_revoked_and_expired_grants_are_410(
    api_client, auth_headers, db_session, patient
):
    token, recipient, issued = await _provision(
        api_client, auth_headers, db_session, patient
    )
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 200

    revoked = await api_client.delete(
        f"{_base(patient)}/recipients/{recipient['id']}/grants/{issued['id']}",
        headers=auth_headers,
    )
    assert revoked.status_code == 204, revoked.text
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )

    # Novo link, agora expirado por relógio.
    issued = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
        json={"expiresInDays": 1, "expectedRecipientVersion": 1},
    )
    assert issued.status_code == 201, issued.text
    fresh_token = issued.json()["url"].split("#token=", 1)[1]
    grant = await db_session.scalar(
        select(FamilyPortalGrant).where(
            FamilyPortalGrant.id == UUID(issued.json()["id"])
        )
    )
    # Simula relógio: grant antigo já vencido (CHECK expiry preservado).
    grant.created_at = datetime.now(UTC) - timedelta(days=10)
    grant.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: fresh_token})
    )


async def test_portal_patient_and_recipient_states_are_410(
    api_client, auth_headers, db_session, patient, professional
):
    token, recipient, _ = await _provision(
        api_client, auth_headers, db_session, patient
    )
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 200

    # Retirada do destinatário (v1 → v2, autorização v2).
    withdrawn = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/withdraw",
        headers=auth_headers,
        json={"reason": "professional_decision"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )

    # Nova autorização reativa (v3); reabilitar portal não é necessário.
    reactivated = await api_client.put(
        f"{_base(patient)}/recipients/{recipient['caregiverId']}",
        headers=auth_headers,
        json={
            "expectedVersion": 2,
            "appointmentsEnabled": False,
            "familyAuthorization": _authorization_body()["familyAuthorization"],
        },
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["version"] == 3
    reissued = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
        json={"expiresInDays": 30, "expectedRecipientVersion": 3},
    )
    assert reissued.status_code == 201, reissued.text
    token2 = reissued.json()["url"].split("#token=", 1)[1]
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token2})
    ).status_code == 200

    # Portal desabilitado.
    disabled = await api_client.post(
        f"{_base(patient)}/disable", headers=auth_headers, json={}
    )
    assert disabled.status_code == 200
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token2})
    )

    # Paciente inativo com portal reabilitado e link novo (época nova).
    await api_client.put(
        _base(patient),
        headers=auth_headers,
        json={"enabled": True, "expectedVersion": 2},
    )
    reissued2 = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
        json={"expiresInDays": 30, "expectedRecipientVersion": 3},
    )
    assert reissued2.status_code == 201, reissued2.text
    token3 = reissued2.json()["url"].split("#token=", 1)[1]
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token3})
    ).status_code == 200
    patient.status = "inativo"
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token3})
    )


async def test_owner_and_epoch_states_are_410(
    api_client, auth_headers, db_session, patient, professional
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    # Época do portal divergente (ex.: bump administrativo).
    portal = await db_session.scalar(select(FamilyPortal))
    portal.access_version += 1
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )

    # Época da conta F14 divergente (desativação incrementa; reativação não reduz).
    await db_session.refresh(professional)
    professional.family_portal_access_version += 1
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )

    # Dono desativado → mesmo 410 (nunca 401/403 no fluxo público).
    professional.is_disabled = True
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )


async def test_removed_caregiver_is_410(
    api_client, auth_headers, db_session, patient, professional
):
    token, recipient, _ = await _provision(
        api_client, auth_headers, db_session, patient
    )

    # Responsável desvinculado (hook de exclusão): ativo→false + FK solta.
    from app.services import family_portal_access

    await family_portal_access.withdraw_recipient_for_caregiver(
        db_session,
        patient_id=patient.id,
        caregiver_id=UUID(recipient["caregiverId"]),
        actor=professional,
        reason="caregiver_removed",
        clear_caregiver_link=True,
    )
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )
    # O histórico privado continua íntegro (trilha append-only preservada).
    events = await api_client.get(f"{_base(patient)}/events", headers=auth_headers)
    assert events.status_code == 200
    assert any(
        item["type"] == "recipient_withdrawn" for item in events.json()["items"]
    )


async def test_owner_transfer_does_not_transfer_the_portal(
    api_client, auth_headers, db_session, patient, professional
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)
    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 200

    other = await _professional(db_session, email="novo-dono@example.com")
    patient.professional_id = other.id
    await db_session.commit()
    await _assert_gone(
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    )
    # O novo dono não herda a administração do portal: enxerga a
    # representação "sem portal" e não pode habilitar em cima do alheio.
    denied = await api_client.get(_base(patient), headers=_headers(other))
    assert denied.status_code == 200, denied.text
    assert denied.json()["id"] is None
    assert denied.json()["enabled"] is False
    blocked = await api_client.put(
        _base(patient),
        headers=_headers(other),
        json={"enabled": True, "expectedVersion": 1},
    )
    assert blocked.status_code == 409


# --------------------------------------------------------------------------- #
# Namespace exclusivo, métodos e leitura sem escrita
# --------------------------------------------------------------------------- #


async def test_f14_token_is_not_accepted_as_f16_token(
    api_client, auth_headers, db_session, patient, professional
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    program = HomeProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Programa",
        status="active",
        version=2,
        starts_on=TODAY - timedelta(days=1),
        ends_on=TODAY + timedelta(days=13),
        timezone="America/Sao_Paulo",
    )
    db_session.add(program)
    await db_session.flush()
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    db_session.add(
        HomeProgramGrant(
            program_id=program.id,
            caregiver_id=caregiver.id,
            caregiver_name_snapshot="Maria",
            caregiver_relation_snapshot="Mãe",
            token_hash=hash_token("token-f16"),
            expires_at=datetime.now(UTC) + timedelta(days=7),
            created_by_professional_id=professional.id,
        )
    )
    await db_session.commit()

    # Token F14 no header F16 → 410 do F16.
    f16_response = await api_client.get(
        HOME_PROGRAM_PUBLIC, headers={"X-Home-Program-Token": token}
    )
    assert f16_response.status_code == 410, f16_response.text
    # Token F16 no header F14 → 410 do F14 (mesmo detail genérico deste portal).
    f14_response = await api_client.get(
        PUBLIC, headers={TOKEN_HEADER: "token-f16"}
    )
    await _assert_gone(f14_response)


async def test_public_route_has_no_mutating_methods(
    api_client, auth_headers, db_session, patient
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    for method in ("post", "put", "patch", "delete"):
        response = await api_client.request(method.upper(), PUBLIC, json={})
        assert response.status_code == 405, (method, response.text)
    head = await api_client.head(PUBLIC)
    assert head.status_code == 405  # HEAD não é roteado para GET neste FastAPI

    assert (
        await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    ).status_code == 200


async def test_public_reads_never_create_rows_or_receipts(
    api_client, auth_headers, db_session, patient
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    async def snapshot() -> tuple[int, int, int, int]:
        return tuple(
            [
                await db_session.scalar(
                    select(func.count()).select_from(model)
                )
                for model in (
                    FamilyPortal,
                    FamilyPortalRecipient,
                    FamilyPortalGrant,
                    FamilyPortalEvent,
                )
            ]
        )

    before = await snapshot()
    for _ in range(3):
        assert (
            await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
        ).status_code == 200
    await api_client.get(PUBLIC, headers={TOKEN_HEADER: "invalido"})
    assert await snapshot() == before


async def test_stale_professional_cookie_neither_blocks_nor_leaks(
    api_client, auth_headers, db_session, patient, professional
):
    token, _, _ = await _provision(api_client, auth_headers, db_session, patient)

    # Assinatura cancelada não muda nada para a família (sem 403 financeiro).
    professional.subscription_status = "canceled"
    await db_session.commit()
    stale_cookie = {ACCESS_COOKIE: create_access_token(professional.id)}

    with_cookie = await api_client.get(
        PUBLIC, headers={TOKEN_HEADER: token}, cookies=stale_cookie
    )
    assert with_cookie.status_code == 200, with_cookie.text
    without_cookie = await api_client.get(
        PUBLIC, headers={TOKEN_HEADER: token}
    )
    assert with_cookie.json() == without_cookie.json()
    assert "assinatura" not in with_cookie.text.lower()

    # O cookie stale não substitui o token: sem token continua 410.
    await _assert_gone(
        await api_client.get(PUBLIC, cookies=stale_cookie)
    )
    # Bearer profissional também não é fallback.
    await _assert_gone(
        await api_client.get(PUBLIC, headers=_headers(professional))
    )


async def test_public_projection_and_listings_never_expose_the_token(
    api_client, auth_headers, db_session, patient
):
    token, recipient, issued = await _provision(
        api_client, auth_headers, db_session, patient
    )
    stored = await db_session.scalar(select(FamilyPortalGrant))
    assert stored.token_hash == hash_token(token)

    root = await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    assert root.status_code == 200
    assert '"tea"' not in root.text  # diagnóstico cadastral nunca sai
    assert "11988887777" not in root.text  # contato do responsável nunca sai

    grants = await api_client.get(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
    )
    assert token not in grants.text
    assert stored.token_hash not in grants.text

    events = await api_client.get(f"{_base(patient)}/events", headers=auth_headers)
    assert token not in events.text
    assert stored.token_hash not in events.text
    # Eventos de grant não carregam payload de autorização.
    for item in events.json()["items"]:
        if item["type"] != "recipient_authorized":
            assert item["authorization"] is None


async def test_private_extra_fields_and_invalid_payloads_are_rejected(
    api_client, auth_headers, db_session, patient
):
    token, recipient, _ = await _provision(
        api_client, auth_headers, db_session, patient
    )
    base = _base(patient)

    unknown_grant = await api_client.post(
        f"{base}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": 1,
            "url": "https://malicioso",
        },
    )
    assert unknown_grant.status_code == 422

    unknown_settings = await api_client.patch(
        f"{base}/recipients/{recipient['id']}",
        headers=auth_headers,
        json={"expectedVersion": 1, "appointmentsEnabled": True, "active": False},
    )
    assert unknown_settings.status_code == 422

    scalar_enabled = await api_client.put(
        base, headers=auth_headers, json={"enabled": "sim", "expectedVersion": 1}
    )
    assert scalar_enabled.status_code == 422

    assert token  # provisionado sem vazamentos acima
