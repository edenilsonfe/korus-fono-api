"""F14 onda 1 — lifecycle do portal: hooks de conta/paciente/responsável e
matriz de entitlement read-only (Tarefa 1.2 do integrador).

Prova que:
- desativar a conta incrementa a época própria do portal (F14) na MESMA
  transação de ``is_disabled`` e mata os links emitidos; reativar NÃO reduz o
  contador nem ressuscita tokens;
- paciente → ``inativo`` desativa o portal, incrementa a época e revoga os
  grants ANTES do cancelamento da agenda (que commita internamente);
- mudança efetiva de identidade/contato do responsável retira a autorização
  (conservador); editar apenas notas não muda nada; exclusão solta o vínculo
  antes do DELETE;
- em read-only: as quatro ações protetivas continuam disponíveis e as demais
  mutações do portal ficam bloqueadas (403 entitlement), leitura intacta.

SQLite em memória; as corridas reais ficam no gate PostgreSQL
(``test_family_portal_concurrency.py``).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_password
from app.models.caregiver import Caregiver
from app.models.family_portal import (
    FamilyPortal,
    FamilyPortalGrant,
    FamilyPortalRecipient,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.admin_professional_service import AdminProfessionalService

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


async def _second_professional(db_session) -> Professional:
    professional = Professional(
        email=f"actor-{datetime.now(UTC).timestamp()}@example.com",
        password_hash=hash_password("testpass123"),
        name="Curadoria Teste",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


async def _caregiver(db_session, patient: Patient, *, name: str = "Responsável") -> Caregiver:
    caregiver = Caregiver(patient_id=patient.id, name=name, relation="Mãe", is_primary=True)
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


def _base(patient: Patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _enable(api_client, headers, patient: Patient) -> None:
    response = await api_client.put(
        _base(patient), headers=headers, json={"enabled": True, "expectedVersion": 1}
    )
    assert response.status_code == 200, response.text


async def _authorize(api_client, headers, patient: Patient, caregiver: Caregiver) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=headers,
        json={
            "expectedVersion": None,
            "appointmentsEnabled": False,
            "familyAuthorization": {
                "authorizedAt": (
                    datetime.now(UTC) - timedelta(days=1)
                ).isoformat(),
                "reference": "Termo de autorização arquivado",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue(api_client, headers, patient: Patient, recipient_id: str) -> tuple[str, str]:
    response = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/grants",
        headers=headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": 1,
            "rotateFromGrantId": None,
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return data["url"].split("#token=", 1)[1], data["id"]


async def _public_status(api_client, token: str) -> int:
    response = await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
    return response.status_code


async def test_account_disable_increments_epoch_and_kills_links(
    api_client, auth_headers, db_session, patient, professional
):
    caregiver = await _caregiver(db_session, patient)
    headers = _headers(professional)
    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver)
    token, _grant_id = await _issue(api_client, headers, patient, recipient["id"])
    assert await _public_status(api_client, token) == 200

    actor = await _second_professional(db_session)
    service = AdminProfessionalService(db_session)
    await service.disable(actor=actor, professional_id=professional.id, reason="teste")

    await db_session.refresh(professional)
    assert professional.is_disabled is True
    assert professional.family_portal_access_version == 1

    # O link emitido antes da desativação morreu para sempre...
    assert await _public_status(api_client, token) == 410

    # ...e reativar NÃO reduz o contador nem ressuscita o token.
    await service.enable(actor=actor, professional_id=professional.id, reason="teste")
    await db_session.refresh(professional)
    assert professional.family_portal_access_version == 1
    assert await _public_status(api_client, token) == 410


async def test_patient_inactive_revokes_portal_before_agenda_cancellation(
    api_client, auth_headers, db_session, patient, professional
):
    caregiver = await _caregiver(db_session, patient)
    headers = _headers(professional)
    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver)
    token, _grant_id = await _issue(api_client, headers, patient, recipient["id"])

    updated = await api_client.patch(
        f"/api/v1/patients/{patient.id}",
        headers=headers,
        json={"status": "inativo"},
    )
    assert updated.status_code == 200, updated.text

    portal = await db_session.scalar(
        select(FamilyPortal).where(FamilyPortal.patient_id == patient.id)
    )
    await db_session.refresh(portal)
    assert portal.enabled is False
    assert portal.access_version == 1
    live = await db_session.scalar(
        select(func.count())
        .select_from(FamilyPortalGrant)
        .where(FamilyPortalGrant.revoked_at.is_(None))
    )
    assert live == 0
    assert await _public_status(api_client, token) == 410


async def test_caregiver_identity_change_withdraws_but_notes_do_not(
    api_client, auth_headers, db_session, patient, professional
):
    caregiver = await _caregiver(db_session, patient, name="Maria")
    headers = _headers(professional)
    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver)
    token, _grant_id = await _issue(api_client, headers, patient, recipient["id"])

    # Notas não são identidade/contato: nada muda.
    notes = await api_client.patch(
        f"/api/v1/patients/{patient.id}/caregivers/{caregiver.id}",
        headers=headers,
        json={"notes": "Observação interna"},
    )
    assert notes.status_code == 200, notes.text
    assert await _public_status(api_client, token) == 200

    # Telefone efetivamente alterado: retirada conservadora + link morto.
    phone = await api_client.patch(
        f"/api/v1/patients/{patient.id}/caregivers/{caregiver.id}",
        headers=headers,
        json={"phone": "11988887777"},
    )
    assert phone.status_code == 200, phone.text
    stored = await db_session.get(FamilyPortalRecipient, UUID(recipient["id"]))
    await db_session.refresh(stored)
    assert stored.active is False
    assert await _public_status(api_client, token) == 410


async def test_caregiver_delete_withdraws_and_clears_link(
    api_client, auth_headers, db_session, patient, professional
):
    caregiver = await _caregiver(db_session, patient)
    headers = _headers(professional)
    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver)
    token, _grant_id = await _issue(api_client, headers, patient, recipient["id"])

    deleted = await api_client.delete(
        f"/api/v1/patients/{patient.id}/caregivers/{caregiver.id}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    stored = await db_session.get(FamilyPortalRecipient, UUID(recipient["id"]))
    await db_session.refresh(stored)
    assert stored.active is False
    assert stored.caregiver_id is None
    assert await _public_status(api_client, token) == 410


async def test_read_only_allows_protective_actions_and_blocks_mutations(
    api_client,
    auth_headers,
    db_session,
    patient,
    professional,
    isolate_entitlement_database,
):
    caregiver = await _caregiver(db_session, patient)
    headers = _headers(professional)
    await _enable(api_client, headers, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver)
    token, grant_id = await _issue(api_client, headers, patient, recipient["id"])

    # Conteúdo editorial da onda 2: um aviso publicado para a família (sem
    # fonte clínica — exercita publish/withdraw/remoção de destinatário).
    created_item = await api_client.post(
        f"{_base(patient)}/items",
        headers=headers,
        json={
            "kind": "notice",
            "content": {
                "title": "Aviso de teste",
                "body": "Traga o caderno na próxima sessão.",
                "expiresInDays": 7,
            },
            "recipientIds": [recipient["id"]],
        },
    )
    assert created_item.status_code == 201, created_item.text
    item_id = created_item.json()["id"]
    published_item = await api_client.post(
        f"{_base(patient)}/items/{item_id}/publish",
        headers=headers,
        json={"expectedVersion": created_item.json()["version"], "reviewed": True},
    )
    assert published_item.status_code == 200, published_item.text

    professional.subscription_status = "canceled"
    await db_session.commit()

    # Leitura pública continua válida em read-only...
    assert await _public_status(api_client, token) == 200

    # ...mutações de conteúdo ficam bloqueadas pelo entitlement...
    blocked_enable = await api_client.put(
        _base(patient),
        headers=auth_headers,
        json={"enabled": True, "expectedVersion": 2},
    )
    assert blocked_enable.status_code == 403
    blocked_recipient = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver.id}",
        headers=auth_headers,
        json={
            "expectedVersion": recipient["version"],
            "appointmentsEnabled": True,
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reference": "Termo",
                "reviewed": True,
            },
        },
    )
    assert blocked_recipient.status_code == 403
    blocked_issue = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/grants",
        headers=auth_headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": recipient["version"],
            "rotateFromGrantId": grant_id,
        },
    )
    assert blocked_issue.status_code == 403
    blocked_create_item = await api_client.post(
        f"{_base(patient)}/items",
        headers=auth_headers,
        json={
            "kind": "notice",
            "content": {"title": "x", "body": "y", "expiresInDays": 7},
            "recipientIds": [recipient["id"]],
        },
    )
    assert blocked_create_item.status_code == 403
    blocked_patch_item = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=auth_headers,
        json={
            "expectedVersion": 1,
            "content": {"title": "x", "body": "y", "expiresInDays": 7},
        },
    )
    assert blocked_patch_item.status_code == 403

    # ...e as quatro ações protetivas seguem disponíveis (exceções exatas).
    appointments_off = await api_client.delete(
        f"{_base(patient)}/recipients/{recipient['id']}/appointments",
        headers=auth_headers,
    )
    assert appointments_off.status_code == 204
    revoked = await api_client.delete(
        f"{_base(patient)}/recipients/{recipient['id']}/grants/{grant_id}",
        headers=auth_headers,
    )
    assert revoked.status_code == 204
    item_recipient_removed = await api_client.delete(
        f"{_base(patient)}/items/{item_id}/recipients/{recipient['id']}",
        headers=auth_headers,
    )
    assert item_recipient_removed.status_code == 204
    item_withdrawn = await api_client.post(
        f"{_base(patient)}/items/{item_id}/withdraw",
        headers=auth_headers,
        json={},
    )
    assert item_withdrawn.status_code == 200, item_withdrawn.text
    withdrawn = await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/withdraw",
        headers=auth_headers,
        json={"reason": "professional_decision"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    disabled = await api_client.post(
        f"{_base(patient)}/disable", headers=auth_headers, json={}
    )
    assert disabled.status_code == 200, disabled.text

    assert await _public_status(api_client, token) == 410
