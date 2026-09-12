"""F14 onda 2 — camada editorial privada (itens, revisões, público e prévia).

Cobre §3.3–3.4: rascunho oculto do público, publish/revisão atômicos, público
explícito por destinatário (documento de A nunca aparece para B), fontes
alheias (404), fingerprint/versão stale (409), aviso que vence por leitura,
mudança clínica que não altera o conteúdo publicado, retiradas protetivas
(item, destinatário do item e destinatário do portal) e escopo do dono dos
kinds material/report (fontes reais ficam nos arquivos da onda 3). SQLite em
memória; as corridas reais ficam no gate PostgreSQL
(``test_family_portal_concurrency``).
"""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_password
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.family_portal import (
    FamilyPortalEvent,
    FamilyPortalGrant,
)
from app.models.family_portal_content import (
    FamilyPortalItem,
    FamilyPortalItemAudience,
    FamilyPortalItemRevision,
)
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session

TODAY = date.today()
TOKEN_HEADER = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"
CLINIC_TZ = ZoneInfo("America/Sao_Paulo")


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


def _clinic_date(value: datetime) -> date:
    return _as_utc(value).astimezone(CLINIC_TZ).date()


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


async def _foreign_patient(db_session, professional: Professional) -> Patient:
    patient = Patient(
        professional_id=professional.id,
        name="Paciente alheio",
        birth_date=date(2019, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


async def _session_row(
    db_session,
    patient: Patient,
    professional: Professional,
    *,
    days_ago: int = 1,
    appointment: Appointment | None = None,
    session_type: str = "Terapia de linguagem",
    notes: str = "Notas clínicas privadas",
) -> Session:
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        appointment_id=appointment.id if appointment is not None else None,
        date=datetime.now(UTC) - timedelta(days=days_ago),
        duration=50,
        type=session_type,
        objectives=[],
        notes=notes,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _goal_row(
    db_session,
    patient: Patient,
    professional: Professional,
    *,
    title: str = "Fala espontânea",
) -> Goal:
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title=title,
        area="Linguagem",
        progress=40,
        start_date=TODAY,
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _appointment_row(
    db_session,
    patient: Patient,
    professional: Professional,
    *,
    status_value: str = "concluido",
    days: int = 1,
) -> Appointment:
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=(datetime.now(UTC) + timedelta(days=days)).date(),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status=status_value,
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)
    return appointment


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


async def _authorize(
    api_client, headers, patient: Patient, caregiver_id, *, appointments: bool = False
) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json={
            "appointmentsEnabled": appointments,
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


async def _issue(api_client, headers, patient: Patient, recipient_id) -> tuple[str, dict]:
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
    return data["url"].split("#token=", 1)[1], data


async def _create_item(
    api_client,
    headers,
    patient: Patient,
    *,
    kind: str,
    source,
    content: dict,
    recipient_ids: list[str] | None = None,
):
    return await api_client.post(
        f"{_base(patient)}/items",
        headers=headers,
        json={
            "kind": kind,
            "source": source,
            "content": content,
            "recipientIds": recipient_ids or [],
        },
    )


async def _get_private_item(api_client, headers, patient: Patient, item_id) -> dict:
    response = await api_client.get(
        f"{_base(patient)}/items/{item_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _publish(
    api_client,
    headers,
    patient: Patient,
    item_id,
    *,
    expected_version: int,
    fingerprint: str | None = None,
):
    return await api_client.post(
        f"{_base(patient)}/items/{item_id}/publish",
        headers=headers,
        json={
            "expectedVersion": expected_version,
            "expectedSourceFingerprint": fingerprint,
            "reviewed": True,
        },
    )


async def _public_items(api_client, token: str, kind: str):
    return await api_client.get(
        f"{PUBLIC}/items?kind={kind}", headers={TOKEN_HEADER: token}
    )


async def _counts(db_session) -> dict:
    async def count(model):
        return int(
            await db_session.scalar(select(func.count()).select_from(model)) or 0
        )

    return {
        "items": await count(FamilyPortalItem),
        "revisions": await count(FamilyPortalItemRevision),
        "audiences": await count(FamilyPortalItemAudience),
        "events": await count(FamilyPortalEvent),
        "grants": await count(FamilyPortalGrant),
    }


# --------------------------------------------------------------------------- #
# Ciclo editorial: rascunho → revisão → público explícito
# --------------------------------------------------------------------------- #


async def test_draft_is_hidden_and_publish_reaches_only_selected_recipient(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={
            "title": "Nossa sessão de terça",
            "body": "Trabalhamos a fala espontânea com brincadeiras.",
        },
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["status"] == "draft"
    assert data["version"] == 1 and data["publishedVersion"] is None
    assert data["hasUnpublishedChanges"] is True
    assert data["sourceChanged"] is False
    assert data["source"]["sessionId"] == str(session_row.id)
    assert len(data["sourceFingerprint"]) == 64
    assert data["draftRecipientIds"] == [recipient["id"]]
    item_id = data["id"]

    # Rascunho NUNCA aparece: nem na listagem pública, nem no detalhe, nem na
    # prévia privada; e o GET público não cria linha alguma.
    listing = await _public_items(api_client, token, "session_summary")
    assert listing.status_code == 200, listing.text
    assert listing.json() == {"items": [], "total": 0, "page": 1, "limit": 20}
    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.status_code == 404
    preview = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=session_summary",
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["total"] == 0
    assert (await _counts(db_session))["revisions"] == 0

    published = await _publish(
        api_client,
        headers,
        patient,
        item_id,
        expected_version=1,
        fingerprint=data["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text
    published_data = published.json()
    assert published_data["status"] == "published"
    assert published_data["publishedVersion"] == 1
    assert published_data["hasUnpublishedChanges"] is False

    listing = await _public_items(api_client, token, "session_summary")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    summary = body["items"][0]
    assert set(summary.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "sessionOn",
        "familyStatus",
        "available",
    }
    assert summary["kind"] == "session_summary"
    assert summary["title"] == "Nossa sessão de terça"
    assert summary["sessionOn"] == _clinic_date(session_row.date).isoformat()
    assert summary["familyStatus"] is None
    assert summary["available"] is True

    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert set(detail_body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "body",
        "sessionOn",
    }
    assert detail_body["body"] == "Trabalhamos a fala espontânea com brincadeiras."
    assert detail_body["sessionOn"] == _clinic_date(session_row.date).isoformat()
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.headers["referrer-policy"] == "no-referrer"
    assert detail.headers["x-content-type-options"] == "nosniff"

    # Nada de IDs de fonte, fingerprint, notas clínicas ou revisão inteira.
    for forbidden in (
        "sessionId",
        "evolutionId",
        "sourceFingerprint",
        str(session_row.id),
        "Notas clínicas privadas",
        "recipientIds",
    ):
        assert forbidden not in detail.text

    # A prévia privada usa o MESMO envelope/allowlist do GET público.
    preview = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=session_summary",
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == listing.json()

    # O contador do portal reflete a publicação; trilha registra o evento.
    portal = await api_client.get(_base(patient), headers=headers)
    assert portal.json()["publishedItemCount"] == 1
    events = await api_client.get(f"{_base(patient)}/events", headers=headers)
    published_events = [
        item
        for item in events.json()["items"]
        if item["type"] == "item_published"
    ]
    assert len(published_events) == 1
    assert published_events[0]["itemId"] == item_id


async def test_published_revision_is_immutable_and_draft_edits_do_not_leak(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo 1", "body": "Texto aprovado."},
        recipient_ids=[recipient["id"]],
    )
    item_id = created.json()["id"]
    fingerprint = created.json()["sourceFingerprint"]
    assert (
        await _publish(
            api_client, headers, patient, item_id,
            expected_version=1, fingerprint=fingerprint,
        )
    ).status_code == 200

    patched = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={
            "expectedVersion": 1,
            "content": {"title": "Resumo 2", "body": "Rascunho novo."},
        },
    )
    assert patched.status_code == 200, patched.text
    patched_data = patched.json()
    assert patched_data["version"] == 2
    assert patched_data["publishedVersion"] == 1
    assert patched_data["hasUnpublishedChanges"] is True

    # A versão pública vigente NÃO muda com a edição do rascunho.
    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.json()["title"] == "Resumo 1"
    assert detail.json()["body"] == "Texto aprovado."

    # No-op: replay com a versão corrente não versiona de novo.
    replay = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={
            "expectedVersion": 2,
            "content": {"title": "Resumo 2", "body": "Rascunho novo."},
        },
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 2

    stale = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={"expectedVersion": 1, "content": {"title": "x", "body": "y"}},
    )
    assert stale.status_code == 409

    published = await _publish(
        api_client, headers, patient, item_id,
        expected_version=2, fingerprint=patched_data["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text
    assert published.json()["publishedVersion"] == 2
    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.json()["title"] == "Resumo 2"

    # Histórico append-only: as duas revisões continuam disponíveis.
    revisions = await api_client.get(
        f"{_base(patient)}/items/{item_id}/revisions", headers=headers
    )
    assert revisions.status_code == 200, revisions.text
    payload = revisions.json()
    assert payload["total"] == 2
    newest, oldest = payload["items"]
    assert newest["version"] == 2 and oldest["version"] == 1
    assert newest["content"]["body"] == "Rascunho novo."
    assert oldest["content"]["body"] == "Texto aprovado."
    assert newest["recipientIds"] == [recipient["id"]]
    assert newest["publishedByProfessionalId"] == str(professional.id)


async def test_publish_is_atomic_and_revalidates_inputs(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    _ = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo", "body": "Texto."},
        recipient_ids=[recipient["id"]],
    )
    item_id = created.json()["id"]
    fingerprint = created.json()["sourceFingerprint"]

    # Versão stale, fingerprint errado e fingerprint ausente → nada persiste.
    stale_version = await _publish(
        api_client, headers, patient, item_id,
        expected_version=2, fingerprint=fingerprint,
    )
    assert stale_version.status_code == 409
    wrong_fingerprint = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint="0" * 64,
    )
    assert wrong_fingerprint.status_code == 409
    missing_fingerprint = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=None,
    )
    assert missing_fingerprint.status_code == 422
    reject_review = await api_client.post(
        f"{_base(patient)}/items/{item_id}/publish",
        headers=headers,
        json={
            "expectedVersion": 1,
            "expectedSourceFingerprint": fingerprint,
            "reviewed": False,
        },
    )
    assert reject_review.status_code == 422
    counts = await _counts(db_session)
    assert counts["revisions"] == 0 and counts["audiences"] == 0
    stored = await db_session.get(FamilyPortalItem, UUID(item_id))
    await db_session.refresh(stored)
    assert stored.status == "draft" and stored.version == 1

    ok = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=fingerprint,
    )
    assert ok.status_code == 200, ok.text

    # Publicar de novo sem mudanças é no-op (sem nova revisão/reuso do aviso).
    noop = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=ok.json()["sourceFingerprint"],
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["publishedVersion"] == 1
    counts = await _counts(db_session)
    assert counts["revisions"] == 1 and counts["audiences"] == 1


async def test_publish_requires_recipients_and_valid_publish_list(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    other = await _caregiver(db_session, patient, name="Bia", relation="Pai")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    other_recipient = await _authorize(api_client, headers, patient, other.id)
    session_row = await _session_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo", "body": "Texto."},
        recipient_ids=[],
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    # Lista vazia não publica (422 com orientação).
    empty = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=created.json()["sourceFingerprint"],
    )
    assert empty.status_code == 422
    assert "responsável" in empty.json()["detail"].lower()

    # Duplicatas → 422; destinatário desconhecido → 404; versão errada → 409.
    duplicated = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={
            "expectedVersion": 1,
            "recipientIds": [recipient["id"], recipient["id"]],
        },
    )
    assert duplicated.status_code == 422
    unknown = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={"expectedVersion": 1, "recipientIds": [str(uuid4())]},
    )
    assert unknown.status_code == 404

    # Destinatário retirado do portal não entra em rascunho (409).
    await api_client.post(
        f"{_base(patient)}/recipients/{other_recipient['id']}/withdraw",
        headers=headers,
        json={"reason": "professional_decision"},
    )
    inactive = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={"expectedVersion": 1, "recipientIds": [other_recipient["id"]]},
    )
    assert inactive.status_code == 409

    # kind/source são imutáveis no PATCH (extra=forbid).
    forbidden = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={"expectedVersion": 1, "kind": "notice"},
    )
    assert forbidden.status_code == 422

    selected = await api_client.patch(
        f"{_base(patient)}/items/{item_id}",
        headers=headers,
        json={"expectedVersion": 1, "recipientIds": [recipient["id"]]},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["version"] == 2
    assert selected.json()["draftRecipientIds"] == [recipient["id"]]

    published = await _publish(
        api_client, headers, patient, item_id,
        expected_version=2, fingerprint=selected.json()["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text


async def test_document_of_a_never_appears_for_b(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver_a = await _caregiver(db_session, patient, name="Ana")
    caregiver_b = await _caregiver(db_session, patient, name="Bia", relation="Pai")
    recipient_a = await _authorize(api_client, headers, patient, caregiver_a.id)
    recipient_b = await _authorize(api_client, headers, patient, caregiver_b.id)
    token_a, _ = await _issue(api_client, headers, patient, recipient_a["id"])
    token_b, _ = await _issue(api_client, headers, patient, recipient_b["id"])
    session_row = await _session_row(db_session, patient, professional)
    goal = await _goal_row(db_session, patient, professional)

    item_a = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Somente para Ana", "body": "Conteúdo de A."},
        recipient_ids=[recipient_a["id"]],
    )
    assert item_a.status_code == 201, item_a.text
    assert (
        await _publish(
            api_client, headers, patient, item_a.json()["id"],
            expected_version=1,
            fingerprint=item_a.json()["sourceFingerprint"],
        )
    ).status_code == 200

    item_b = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={
            "title": "Meta da Bia",
            "body": "Vamos praticar em casa.",
            "familyStatus": "practicing",
        },
        recipient_ids=[recipient_b["id"]],
    )
    assert item_b.status_code == 201, item_b.text
    assert (
        await _publish(
            api_client, headers, patient, item_b.json()["id"],
            expected_version=1,
            fingerprint=item_b.json()["sourceFingerprint"],
        )
    ).status_code == 200

    listing_a = (await _public_items(api_client, token_a, "session_summary")).json()
    listing_b = (await _public_items(api_client, token_b, "session_summary")).json()
    assert [item["title"] for item in listing_a["items"]] == ["Somente para Ana"]
    assert listing_b["total"] == 0

    goals_a = (await _public_items(api_client, token_a, "goal")).json()
    goals_b = (await _public_items(api_client, token_b, "goal")).json()
    assert goals_a["total"] == 0
    assert goals_b["items"][0]["familyStatus"] == "practicing"

    # Detalhe cruzado nunca entrega o documento do outro.
    cross_a = await api_client.get(
        f"{PUBLIC}/items/{item_b.json()['id']}", headers={TOKEN_HEADER: token_a}
    )
    cross_b = await api_client.get(
        f"{PUBLIC}/items/{item_a.json()['id']}", headers={TOKEN_HEADER: token_b}
    )
    assert cross_a.status_code == 404
    assert cross_b.status_code == 404

    # Prévia por destinatário reflete o público efetivo de cada um.
    preview_a = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient_a['id']}&kind=goal",
        headers=headers,
    )
    assert preview_a.status_code == 200
    assert preview_a.json()["total"] == 0
    preview_b = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient_b['id']}&kind=goal",
        headers=headers,
    )
    assert preview_b.json()["total"] == 1
    assert preview_b.json()["items"][0]["title"] == "Meta da Bia"


# --------------------------------------------------------------------------- #
# Fontes privadas e fingerprints
# --------------------------------------------------------------------------- #


async def test_sources_picker_and_foreign_sources_are_rejected(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    own_session = await _session_row(db_session, patient, professional)
    manual_session = await _session_row(
        db_session, patient, professional, days_ago=3, session_type="Orientação"
    )
    goal = await _goal_row(db_session, patient, professional)

    foreign_professional = await _professional(db_session, email="fora@example.com")
    foreign_patient = await _foreign_patient(db_session, foreign_professional)
    foreign_session = await _session_row(
        db_session, foreign_patient, foreign_professional
    )
    foreign_goal = await _goal_row(db_session, foreign_patient, foreign_professional)

    sessions = await api_client.get(
        f"{_base(patient)}/sources?kind=session", headers=headers
    )
    assert sessions.status_code == 200, sessions.text
    payload = sessions.json()
    assert payload["total"] == 2
    assert [item["id"] for item in payload["items"]] == [
        str(own_session.id),
        str(manual_session.id),
    ]
    first = payload["items"][0]
    assert set(first.keys()) == {
        "id",
        "kind",
        "label",
        "date",
        "sourceFingerprint",
        "eligible",
        "unavailableReason",
    }
    assert first["kind"] == "session"
    assert first["label"] == "Terapia de linguagem"
    assert first["eligible"] is True
    assert len(first["sourceFingerprint"]) == 64

    filtered = await api_client.get(
        f"{_base(patient)}/sources?kind=session&q=orienta", headers=headers
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["id"] == str(manual_session.id)

    goals = await api_client.get(
        f"{_base(patient)}/sources?kind=goal", headers=headers
    )
    assert goals.status_code == 200
    assert goals.json()["items"][0]["label"] == "Fala espontânea"

    # Fontes de material/entrega existem de verdade (onda 3); sem candidatos
    # neste paciente, a resposta é uma listagem vazia legítima.
    for source_kind in ("resource", "reportDelivery"):
        listed = await api_client.get(
            f"{_base(patient)}/sources?kind={source_kind}", headers=headers
        )
        assert listed.status_code == 200, source_kind
        assert listed.json()["items"] == []
    invalid = await api_client.get(
        f"{_base(patient)}/sources?kind=outro", headers=headers
    )
    assert invalid.status_code == 422
    long_query = await api_client.get(
        f"{_base(patient)}/sources?kind=session&q={'x' * 101}", headers=headers
    )
    assert long_query.status_code == 422

    # Criar item com fonte alheia → 404; evolução de outra sessão → 404.
    alien_session = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(foreign_session.id), "evolutionId": None},
        content={"title": "x", "body": "y"},
    )
    assert alien_session.status_code == 404
    alien_goal = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(foreign_goal.id)},
        content={"title": "x", "body": "y", "familyStatus": "practicing"},
    )
    assert alien_goal.status_code == 404

    evolution = Evolution(
        patient_id=patient.id,
        session_id=own_session.id,
        professional_id=professional.id,
        date=own_session.date,
        title="Evolução",
        content="Conteúdo clínico privado",
    )
    db_session.add(evolution)
    await db_session.commit()
    await db_session.refresh(evolution)
    mismatched = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={
            "sessionId": str(manual_session.id),
            "evolutionId": str(evolution.id),
        },
        content={"title": "x", "body": "y"},
    )
    assert mismatched.status_code == 404

    # Sessão futura e consulta não concluída não são elegíveis.
    future = await _session_row(db_session, patient, professional, days_ago=-2)
    future_item = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(future.id), "evolutionId": None},
        content={"title": "x", "body": "y"},
    )
    assert future_item.status_code == 422
    open_appointment = await _appointment_row(
        db_session, patient, professional, status_value="pendente"
    )
    open_session = await _session_row(
        db_session, patient, professional, appointment=open_appointment
    )
    open_item = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(open_session.id), "evolutionId": None},
        content={"title": "x", "body": "y"},
    )
    assert open_item.status_code == 422
    future_candidates = await api_client.get(
        f"{_base(patient)}/sources?kind=session", headers=headers
    )
    assert str(future.id) not in [
        item["id"] for item in future_candidates.json()["items"]
    ]

    # Consulta concluída é elegível e a evolução da MESMA sessão vale.
    done_appointment = await _appointment_row(
        db_session, patient, professional, status_value="concluido"
    )
    done_session = await _session_row(
        db_session, patient, professional, appointment=done_appointment
    )
    done_evolution = Evolution(
        patient_id=patient.id,
        session_id=done_session.id,
        professional_id=professional.id,
        date=done_session.date,
        title="Evolução",
        content="Conteúdo clínico privado",
    )
    db_session.add(done_evolution)
    await db_session.commit()
    await db_session.refresh(done_evolution)
    eligible = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={
            "sessionId": str(done_session.id),
            "evolutionId": str(done_evolution.id),
        },
        content={"title": "x", "body": "y"},
    )
    assert eligible.status_code == 201, eligible.text
    # O fingerprint privado NUNCA carrega o texto clínico bruto.
    assert "Conteúdo clínico privado" not in eligible.text


async def test_stale_clinical_source_never_changes_public_content(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo aprovado", "body": "Texto aprovado."},
        recipient_ids=[recipient["id"]],
    )
    item_id = created.json()["id"]
    fingerprint_v1 = created.json()["sourceFingerprint"]
    assert (
        await _publish(
            api_client, headers, patient, item_id,
            expected_version=1, fingerprint=fingerprint_v1,
        )
    ).status_code == 200

    # A clínica muda DEPOIS da publicação: o público continua no revisado.
    session_row.notes = "Notas novas que ninguém revisou"
    await db_session.commit()

    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.json()["body"] == "Texto aprovado."

    private = await _get_private_item(api_client, headers, patient, item_id)
    assert private["sourceChanged"] is True
    assert private["sourceFingerprint"] != fingerprint_v1

    # Publicar com o fingerprint antigo é 409 (fonte stale).
    stale = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=fingerprint_v1,
    )
    assert stale.status_code == 409
    assert "mudou" in stale.json()["detail"].lower()

    # Reconferida a fonte vigente, republicar é no-op: sem revisão nova.
    refreshed = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=private["sourceFingerprint"],
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["sourceChanged"] is False
    counts = await _counts(db_session)
    assert counts["revisions"] == 1
    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.json()["body"] == "Texto aprovado."


# --------------------------------------------------------------------------- #
# Avisos: vencem por leitura, sem cron
# --------------------------------------------------------------------------- #


async def test_notice_expires_by_reading_without_any_write(
    api_client, auth_headers, db_session, patient
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={
            "title": "Aviso de retorno",
            "body": "Traga o caderno na próxima sessão.",
            "expiresInDays": 1,
        },
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    assert created.json()["source"] is None
    assert created.json()["sourceFingerprint"] is None
    assert created.json()["sourceChanged"] is False
    item_id = created.json()["id"]

    published = await _publish(
        api_client, headers, patient, item_id,
        expected_version=1, fingerprint=None,
    )
    assert published.status_code == 200, published.text

    listing = (await _public_items(api_client, token, "notice")).json()
    assert listing["total"] == 1
    detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.status_code == 200
    body = detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "body",
        "expiresAt",
    }
    expires_at = _as_utc(datetime.fromisoformat(body["expiresAt"]))
    assert abs((expires_at - (datetime.now(UTC) + timedelta(days=1))).total_seconds()) < 300

    # Vence por leitura: nenhum job/escrita — basta o relógio.
    revision = await db_session.scalar(
        select(FamilyPortalItemRevision).where(
            FamilyPortalItemRevision.item_id == UUID(item_id)
        )
    )
    revision.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    counts_before = await _counts(db_session)

    listing = (await _public_items(api_client, token, "notice")).json()
    assert listing["total"] == 0
    expired_detail = await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )
    assert expired_detail.status_code == 404
    preview = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=notice",
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json()["total"] == 0
    stored = await db_session.get(FamilyPortalItem, UUID(item_id))
    await db_session.refresh(stored)
    assert stored.status == "published"
    counts_after = await _counts(db_session)
    assert counts_before == counts_after  # leitura não escreve nada


# --------------------------------------------------------------------------- #
# Retiradas protetivas
# --------------------------------------------------------------------------- #


async def test_withdraw_item_clears_audience_and_keeps_history(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    goal = await _goal_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "Corpo.", "familyStatus": "practicing"},
        recipient_ids=[recipient["id"]],
    )
    item_id = created.json()["id"]
    assert (
        await _publish(
            api_client, headers, patient, item_id,
            expected_version=1,
            fingerprint=created.json()["sourceFingerprint"],
        )
    ).status_code == 200
    assert (await _public_items(api_client, token, "goal")).json()["total"] == 1

    withdrawn = await api_client.post(
        f"{_base(patient)}/items/{item_id}/withdraw", headers=headers, json={}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    data = withdrawn.json()
    assert data["status"] == "withdrawn"
    assert data["version"] == 2 and data["publishedVersion"] == 1
    assert (await _public_items(api_client, token, "goal")).json()["total"] == 0
    assert (
        await api_client.get(
            f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
        )
    ).status_code == 404

    # Idempotente: repetir não versiona nem duplica evento.
    repeated = await api_client.post(
        f"{_base(patient)}/items/{item_id}/withdraw", headers=headers, json={}
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == 2
    counts = await _counts(db_session)
    assert counts["audiences"] == 0
    assert counts["revisions"] == 1  # texto preservado
    events = (
        await api_client.get(f"{_base(patient)}/events", headers=headers)
    ).json()["items"]
    assert len([e for e in events if e["type"] == "item_withdrawn"]) == 1

    # Corpo com campo desconhecido é rejeitado.
    bad_body = await api_client.post(
        f"{_base(patient)}/items/{item_id}/withdraw",
        headers=headers,
        json={"reason": "x"},
    )
    assert bad_body.status_code == 422


async def test_remove_item_recipient_is_protective_and_idempotent(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver_a = await _caregiver(db_session, patient, name="Ana")
    caregiver_b = await _caregiver(db_session, patient, name="Bia", relation="Pai")
    recipient_a = await _authorize(api_client, headers, patient, caregiver_a.id)
    recipient_b = await _authorize(api_client, headers, patient, caregiver_b.id)
    token_a, _ = await _issue(api_client, headers, patient, recipient_a["id"])
    token_b, _ = await _issue(api_client, headers, patient, recipient_b["id"])
    goal = await _goal_row(db_session, patient, professional)

    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "Corpo.", "familyStatus": "achieved"},
        recipient_ids=[recipient_a["id"], recipient_b["id"]],
    )
    item_id = created.json()["id"]
    assert (
        await _publish(
            api_client, headers, patient, item_id,
            expected_version=1,
            fingerprint=created.json()["sourceFingerprint"],
        )
    ).status_code == 200

    removed = await api_client.delete(
        f"{_base(patient)}/items/{item_id}/recipients/{recipient_a['id']}",
        headers=headers,
    )
    assert removed.status_code == 204, removed.text
    assert (await _public_items(api_client, token_a, "goal")).json()["total"] == 0
    assert (await _public_items(api_client, token_b, "goal")).json()["total"] == 1

    private = await _get_private_item(api_client, headers, patient, item_id)
    assert private["draftRecipientIds"] == [recipient_b["id"]]
    assert private["version"] == 2

    # Idempotente: repetir não versiona de novo.
    again = await api_client.delete(
        f"{_base(patient)}/items/{item_id}/recipients/{recipient_a['id']}",
        headers=headers,
    )
    assert again.status_code == 204
    assert (
        await _get_private_item(api_client, headers, patient, item_id)
    )["version"] == 2
    # Destinatário desconhecido no portal → 404.
    missing = await api_client.delete(
        f"{_base(patient)}/items/{item_id}/recipients/{uuid4()}", headers=headers
    )
    assert missing.status_code == 404


async def test_recipient_withdrawal_strips_audiences_and_drafts(
    api_client, auth_headers, db_session, patient, professional
):
    from app.services import family_portal_access

    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver_a = await _caregiver(db_session, patient, name="Ana")
    caregiver_b = await _caregiver(db_session, patient, name="Bia", relation="Pai")
    recipient_a = await _authorize(api_client, headers, patient, caregiver_a.id)
    recipient_b = await _authorize(api_client, headers, patient, caregiver_b.id)
    token_a, _ = await _issue(api_client, headers, patient, recipient_a["id"])
    goal = await _goal_row(db_session, patient, professional)

    published_item = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta A", "body": "Corpo.", "familyStatus": "practicing"},
        recipient_ids=[recipient_a["id"]],
    )
    publish_response = await _publish(
        api_client, headers, patient, published_item.json()["id"],
        expected_version=1,
        fingerprint=published_item.json()["sourceFingerprint"],
    )
    assert publish_response.status_code == 200
    draft_item = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Rascunho A", "body": "Corpo.", "familyStatus": "paused"},
        recipient_ids=[recipient_a["id"], recipient_b["id"]],
    )
    assert draft_item.status_code == 201

    withdrawn = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_a['id']}/withdraw",
        headers=headers,
        json={"reason": "family_request"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    # O link de A morre na retirada; o público vigente também perde A.
    gone = await _public_items(api_client, token_a, "goal")
    assert gone.status_code == 410
    stored = await db_session.scalar(
        select(FamilyPortalItemAudience).where(
            FamilyPortalItemAudience.item_id
            == UUID(published_item.json()["id"])
        )
    )
    assert stored is None
    revisions = (
        await api_client.get(
            f"{_base(patient)}/items/{published_item.json()['id']}/revisions",
            headers=headers,
        )
    ).json()
    assert revisions["total"] == 1  # revisão preservada
    draft = await _get_private_item(
        api_client, headers, patient, draft_item.json()["id"]
    )
    assert draft["draftRecipientIds"] == [recipient_b["id"]]
    assert draft["version"] == 2
    # A remoção da audiência também versiona o item publicado.
    published_private = await _get_private_item(
        api_client, headers, patient, published_item.json()["id"]
    )
    assert published_private["version"] == 2

    # Caminho do caregiver (hook legado) usa o MESMO strip via serviço.
    new_published = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta B", "body": "Corpo.", "familyStatus": "practicing"},
        recipient_ids=[recipient_b["id"]],
    )
    published_response = await _publish(
        api_client, headers, patient, new_published.json()["id"],
        expected_version=1,
        fingerprint=new_published.json()["sourceFingerprint"],
    )
    assert published_response.status_code == 200
    removed = await family_portal_access.withdraw_recipient_for_caregiver(
        db_session,
        patient_id=patient.id,
        caregiver_id=caregiver_b.id,
        actor=professional,
        reason="caregiver_removed",
        clear_caregiver_link=True,
    )
    await db_session.commit()
    assert removed == 1
    remaining = (
        (
            await db_session.execute(
                select(FamilyPortalItemAudience).where(
                    FamilyPortalItemAudience.item_id
                    == UUID(new_published.json()["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []
    events = (
        await api_client.get(f"{_base(patient)}/events", headers=headers)
    ).json()["items"]
    reasons = [
        event["type"]
        for event in events
        if event["itemId"] == new_published.json()["id"]
    ]
    assert "item_recipient_removed" in reasons


# --------------------------------------------------------------------------- #
# Kinds habilitados e escopo do dono (onda 3: material/report por fonte real)
# --------------------------------------------------------------------------- #


async def test_material_and_report_sources_validate_scope_without_creating_anything(
    api_client, auth_headers, db_session, patient
):
    """IDs de fonte desconhecidos para material/report → 404, sem criar nada."""
    headers = auth_headers
    await _enable(api_client, headers, patient)
    for kind, source in (
        ("material", {"resourceId": str(uuid4())}),
        ("report", {"deliveryId": str(uuid4())}),
    ):
        response = await _create_item(
            api_client,
            headers,
            patient,
            kind=kind,
            source=source,
            content={"title": "x"},
        )
        assert response.status_code == 404, (kind, response.text)
    counts = await _counts(db_session)
    assert counts["items"] == 0
    invalid_kind = await _create_item(
        api_client,
        headers,
        patient,
        kind="outro",
        source=None,
        content={"title": "x"},
    )
    assert invalid_kind.status_code == 422


async def test_editorial_layer_is_owner_scoped(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    goal = await _goal_row(db_session, patient, professional)
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "Corpo.", "familyStatus": "practicing"},
        recipient_ids=[recipient["id"]],
    )
    item_id = created.json()["id"]

    other = await _professional(db_session, email="intrusa@example.com")
    other_headers = _headers(other)
    for method, path, body in (
        ("get", f"{_base(patient)}/items", None),
        ("get", f"{_base(patient)}/items/{item_id}", None),
        (
            "get",
            f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=goal",
            None,
        ),
        ("get", f"{_base(patient)}/sources?kind=goal", None),
        ("get", f"{_base(patient)}/items/{item_id}/revisions", None),
        (
            "post",
            f"{_base(patient)}/items",
            {
                "kind": "goal",
                "source": {"goalId": str(goal.id)},
                "content": {
                    "title": "x",
                    "body": "y",
                    "familyStatus": "practicing",
                },
                "recipientIds": [],
            },
        ),
        (
            "post",
            f"{_base(patient)}/items/{item_id}/publish",
            {
                "expectedVersion": 1,
                "expectedSourceFingerprint": created.json()["sourceFingerprint"],
                "reviewed": True,
            },
        ),
        ("post", f"{_base(patient)}/items/{item_id}/withdraw", {}),
        (
            "delete",
            f"{_base(patient)}/items/{item_id}/recipients/{recipient['id']}",
            None,
        ),
    ):
        kwargs = {"headers": other_headers}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(api_client, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.text)


async def test_preview_enforces_portal_recipient_and_patient_states(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient, name="Ana")
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    goal = await _goal_row(db_session, patient, professional)
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "Corpo.", "familyStatus": "practicing"},
        recipient_ids=[recipient["id"]],
    )
    assert (
        await _publish(
            api_client, headers, patient, created.json()["id"],
            expected_version=1,
            fingerprint=created.json()["sourceFingerprint"],
        )
    ).status_code == 200

    unknown = await api_client.get(
        f"{_base(patient)}/preview?recipientId={uuid4()}&kind=goal",
        headers=headers,
    )
    assert unknown.status_code == 404
    missing_kind = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}",
        headers=headers,
    )
    assert missing_kind.status_code == 422

    # Destinatário retirado → 409 (a prévia não simula autorização vencida).
    await api_client.post(
        f"{_base(patient)}/recipients/{recipient['id']}/withdraw",
        headers=headers,
        json={"reason": "professional_decision"},
    )
    withdrawn = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=goal",
        headers=headers,
    )
    assert withdrawn.status_code == 409

    # Portal desabilitado → 409.
    await api_client.post(f"{_base(patient)}/disable", headers=headers, json={})
    disabled = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=goal",
        headers=headers,
    )
    assert disabled.status_code == 409


async def test_portal_item_cap_counts_non_withdrawn_items(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    portal_response = await _enable(api_client, headers, patient)
    portal_id = UUID(portal_response["id"])
    now = datetime.now(UTC)
    db_session.add_all(
        [
            FamilyPortalItem(
                portal_id=portal_id,
                kind="notice",
                status="draft",
                version=1,
                published_version=None,
                draft_content={},
                draft_recipient_ids=[],
                created_by_professional_id=professional.id,
                created_at=now,
                updated_at=now,
            )
            for _ in range(200)
        ]
    )
    await db_session.commit()

    blocked = await _create_item(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={"title": "x", "body": "y", "expiresInDays": 1},
    )
    assert blocked.status_code == 422
    assert "Limite" in blocked.json()["detail"]

    # Retirar um item libera espaço (retirados não contam no teto).
    listing = await api_client.get(
        f"{_base(patient)}/items?limit=1", headers=headers
    )
    victim = listing.json()["items"][0]["id"]
    withdrew = await api_client.post(
        f"{_base(patient)}/items/{victim}/withdraw", headers=headers, json={}
    )
    assert withdrew.status_code == 200, withdrew.text
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={"title": "x", "body": "y", "expiresInDays": 1},
    )
    assert created.status_code == 201, created.text
