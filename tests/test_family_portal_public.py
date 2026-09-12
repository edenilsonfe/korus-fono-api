"""F14 onda 2 — leitura pública de conteúdo e agenda (§3.4).

Cobre: listagem/detalhe apenas do que está publicado e no público vigente do
destinatário, aviso expirado como 404, rascunho/retirado invisíveis, 410
genérico para links inválidos nos novos endpoints, headers públicos, ordem e
paginação, e a garantia de que GET/prefetch não escreve linha alguma (sem
recibo, sem renovação de prazo, sem auditoria). SQLite em memória.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_password
from app.models.caregiver import Caregiver
from app.models.family_portal import FamilyPortalEvent, FamilyPortalGrant
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


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: contador público sempre 'permite'."""
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


async def _caregiver(
    db_session, patient: Patient, *, name: str = "Responsável Teste"
) -> Caregiver:
    caregiver = Caregiver(patient_id=patient.id, name=name, relation="Mãe")
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _session_row(
    db_session, patient: Patient, professional: Professional, *, days_ago: int = 1
) -> Session:
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime.now(UTC) - timedelta(days=days_ago),
        duration=50,
        type="Terapia de linguagem",
        objectives=[],
        notes="Notas clínicas privadas",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _goal_row(
    db_session, patient: Patient, professional: Professional, *, title: str = "Fala"
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


def _base(patient: Patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _enable(api_client, headers, patient: Patient) -> None:
    response = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text


async def _authorize(api_client, headers, patient: Patient, caregiver_id) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json={
            "appointmentsEnabled": False,
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reference": "Termo",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue(api_client, headers, patient: Patient, recipient_id) -> str:
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
    return response.json()["url"].split("#token=", 1)[1]


async def _create_and_publish(
    api_client,
    headers,
    patient: Patient,
    *,
    kind: str,
    source,
    content: dict,
    recipient_ids: list[str],
) -> str:
    created = await api_client.post(
        f"{_base(patient)}/items",
        headers=headers,
        json={
            "kind": kind,
            "source": source,
            "content": content,
            "recipientIds": recipient_ids,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()
    published = await api_client.post(
        f"{_base(patient)}/items/{item['id']}/publish",
        headers=headers,
        json={
            "expectedVersion": 1,
            "expectedSourceFingerprint": item["sourceFingerprint"],
            "reviewed": True,
        },
    )
    assert published.status_code == 200, published.text
    return item["id"]


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


async def test_public_items_allowlist_headers_and_order(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)
    goal = await _goal_row(db_session, patient, professional)

    summary_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo da sessão", "body": "Texto aprovado."},
        recipient_ids=[recipient["id"]],
    )
    goal_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={
            "title": "Meta revisada",
            "body": "Vamos treinar em casa.",
            "familyStatus": "practicing",
        },
        recipient_ids=[recipient["id"]],
    )
    notice_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={
            "title": "Aviso",
            "body": "Sessão de sábado.",
            "expiresInDays": 7,
        },
        recipient_ids=[recipient["id"]],
    )

    # Partição por kind (kind é obrigatório).
    missing_kind = await api_client.get(
        f"{PUBLIC}/items", headers={TOKEN_HEADER: token}
    )
    assert missing_kind.status_code == 422
    invalid_kind = await api_client.get(
        f"{PUBLIC}/items?kind=outro", headers={TOKEN_HEADER: token}
    )
    assert invalid_kind.status_code == 422

    for kind, expected_id in (
        ("session_summary", summary_id),
        ("goal", goal_id),
        ("notice", notice_id),
    ):
        response = await api_client.get(
            f"{PUBLIC}/items?kind={kind}", headers={TOKEN_HEADER: token}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == expected_id
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    # Material/report não têm itens publicados nesta onda: listagens vazias
    # (sem 500 e sem expor nada).
    for reserved in ("material", "report"):
        empty = await api_client.get(
            f"{PUBLIC}/items?kind={reserved}", headers={TOKEN_HEADER: token}
        )
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "total": 0, "page": 1, "limit": 20}

    # Detalhe por kind com os campos do contrato.
    detail = await api_client.get(
        f"{PUBLIC}/items/{summary_id}", headers={TOKEN_HEADER: token}
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "body",
        "sessionOn",
    }
    assert body["body"] == "Texto aprovado."
    from zoneinfo import ZoneInfo

    expected_session_on = (
        _as_utc(session_row.date)
        .astimezone(ZoneInfo("America/Sao_Paulo"))
        .date()
        .isoformat()
    )
    assert body["sessionOn"] == expected_session_on

    goal_detail = await api_client.get(
        f"{PUBLIC}/items/{goal_id}", headers={TOKEN_HEADER: token}
    )
    body = goal_detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "body",
        "familyStatus",
    }
    assert body["familyStatus"] == "practicing"

    notice_detail = await api_client.get(
        f"{PUBLIC}/items/{notice_id}", headers={TOKEN_HEADER: token}
    )
    body = notice_detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "body",
        "expiresAt",
    }
    assert body["available"] is True

    # Ordem decrescente por publishedAt dentro do kind.
    first = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={"title": "Aviso 2", "body": "Segundo.", "expiresInDays": 3},
        recipient_ids=[recipient["id"]],
    )
    listing = await api_client.get(
        f"{PUBLIC}/items?kind=notice", headers={TOKEN_HEADER: token}
    )
    assert [item["id"] for item in listing.json()["items"]] == [first, notice_id]

    # Paginação dentro do autorizado.
    page = await api_client.get(
        f"{PUBLIC}/items?kind=notice&page=1&limit=1",
        headers={TOKEN_HEADER: token},
    )
    assert page.json()["total"] == 2 and len(page.json()["items"]) == 1

    # Nenhum ID interno/fonte/texto clínico vaza em listagem ou detalhe.
    everything = (
        await api_client.get(
            f"{PUBLIC}/items?kind=session_summary", headers={TOKEN_HEADER: token}
        )
    ).text + notice_detail.text
    for forbidden in (
        "sessionId",
        "goalId",
        "sourceFingerprint",
        "sourceMetadata",
        "recipientIds",
        str(session_row.id),
        str(goal.id),
        "Notas clínicas privadas",
        str(recipient["id"]),
        "draftContent",
    ):
        assert forbidden not in everything


async def test_public_invalid_links_are_410_and_out_of_scope_is_404(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver_a = await _caregiver(db_session, patient, name="Ana")
    caregiver_b = await _caregiver(db_session, patient, name="Bia")
    recipient_a = await _authorize(api_client, headers, patient, caregiver_a.id)
    recipient_b = await _authorize(api_client, headers, patient, caregiver_b.id)
    token_a = await _issue(api_client, headers, patient, recipient_a["id"])
    _token_b = await _issue(api_client, headers, patient, recipient_b["id"])
    goal = await _goal_row(db_session, patient, professional)
    item_a = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Somente A", "body": "x", "familyStatus": "practicing"},
        recipient_ids=[recipient_a["id"]],
    )

    gone_message = (
        "Este link está inválido ou indisponível. Peça um novo link à "
        "profissional."
    )
    for path in (
        "/items?kind=goal",
        f"/items/{item_a}",
    ):
        response = await api_client.get(f"{PUBLIC}{path}")
        assert response.status_code == 410, path
        assert response.json()["detail"] == gone_message
        unknown = await api_client.get(
            f"{PUBLIC}{path}", headers={TOKEN_HEADER: "desconhecido"}
        )
        assert unknown.status_code == 410
        assert unknown.json()["detail"] == gone_message

    # Item fora do público do destinatário → MESMO 404 neutro.
    cross = await api_client.get(
        f"{PUBLIC}/items/{item_a}", headers={TOKEN_HEADER: _token_b}
    )
    assert cross.status_code == 404
    assert cross.json()["detail"] == "Conteúdo não encontrado."
    missing = await api_client.get(
        f"{PUBLIC}/items/{uuid4()}", headers={TOKEN_HEADER: token_a}
    )
    assert missing.status_code == 404

    # Outro paciente/portal nunca aparece, mesmo com grant legítimo no próprio.
    other = await _professional(db_session, email="outro@example.com")
    other_patient = Patient(
        professional_id=other.id,
        name="Outra criança",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(other_patient)
    await db_session.commit()
    await db_session.refresh(other_patient)
    await _enable(api_client, _headers(other), other_patient)
    other_caregiver = await _caregiver(db_session, other_patient, name="Outra")
    other_recipient = await _authorize(
        api_client, _headers(other), other_patient, other_caregiver.id
    )
    other_goal = await _goal_row(db_session, other_patient, other)
    alien = await _create_and_publish(
        api_client,
        _headers(other),
        other_patient,
        kind="goal",
        source={"goalId": str(other_goal.id)},
        content={"title": "Alheio", "body": "x", "familyStatus": "practicing"},
        recipient_ids=[other_recipient["id"]],
    )
    peek = await api_client.get(
        f"{PUBLIC}/items/{alien}", headers={TOKEN_HEADER: token_a}
    )
    assert peek.status_code == 404


async def test_withdrawn_and_expired_are_hidden_from_public_reads(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token = await _issue(api_client, headers, patient, recipient["id"])
    goal = await _goal_row(db_session, patient, professional)

    item_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "x", "familyStatus": "paused"},
        recipient_ids=[recipient["id"]],
    )
    assert (await api_client.get(
        f"{PUBLIC}/items?kind=goal", headers={TOKEN_HEADER: token}
    )).json()["total"] == 1

    # Retirado → some da listagem e o detalhe vira 404 (sem revelar estado).
    withdrawn = await api_client.post(
        f"{_base(patient)}/items/{item_id}/withdraw", headers=headers, json={}
    )
    assert withdrawn.status_code == 200
    assert (await api_client.get(
        f"{PUBLIC}/items?kind=goal", headers={TOKEN_HEADER: token}
    )).json()["total"] == 0
    assert (await api_client.get(
        f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
    )).status_code == 404

    # Aviso com prazo vencido também some (por leitura, sem escrita).
    notice_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="notice",
        source=None,
        content={"title": "Aviso", "body": "x", "expiresInDays": 1},
        recipient_ids=[recipient["id"]],
    )
    revision = await db_session.scalar(
        select(FamilyPortalItemRevision).where(
            FamilyPortalItemRevision.item_id == UUID(notice_id)
        )
    )
    revision.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()
    assert (await api_client.get(
        f"{PUBLIC}/items?kind=notice", headers={TOKEN_HEADER: token}
    )).json()["total"] == 0
    assert (await api_client.get(
        f"{PUBLIC}/items/{notice_id}", headers={TOKEN_HEADER: token}
    )).status_code == 404


async def test_public_reads_never_write_rows_or_receipts(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token = await _issue(api_client, headers, patient, recipient["id"])
    session_row = await _session_row(db_session, patient, professional)
    item_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id), "evolutionId": None},
        content={"title": "Resumo", "body": "x"},
        recipient_ids=[recipient["id"]],
    )
    before = await _counts(db_session)

    for _ in range(3):
        assert (
            await api_client.get(PUBLIC, headers={TOKEN_HEADER: token})
        ).status_code == 200
        assert (
            await api_client.get(
                f"{PUBLIC}/items?kind=session_summary",
                headers={TOKEN_HEADER: token},
            )
        ).status_code == 200
        assert (
            await api_client.get(
                f"{PUBLIC}/items/{item_id}", headers={TOKEN_HEADER: token}
            )
        ).status_code == 200
        assert (
            await api_client.get(
                f"{PUBLIC}/appointments", headers={TOKEN_HEADER: token}
            )
        ).status_code == 200
        # Prefetch sem header não pula nada nem cria estado.
        assert (await api_client.get(f"{PUBLIC}/items?kind=goal")).status_code == 410

    after = await _counts(db_session)
    assert before == after

    # HEAD não é roteado para GET neste FastAPI (405) — o invariante é que
    # nenhuma escrita acontece; nenhuma rota HEAD foi adicionada por isso.
    head = await api_client.head(
        f"{PUBLIC}/items?kind=session_summary", headers={TOKEN_HEADER: token}
    )
    assert head.status_code in (405, 200)
    assert await _counts(db_session) == before


async def test_public_route_has_no_mutating_methods_on_content(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token = await _issue(api_client, headers, patient, recipient["id"])
    goal = await _goal_row(db_session, patient, professional)
    item_id = await _create_and_publish(
        api_client,
        headers,
        patient,
        kind="goal",
        source={"goalId": str(goal.id)},
        content={"title": "Meta", "body": "x", "familyStatus": "practicing"},
        recipient_ids=[recipient["id"]],
    )
    for method, path in (
        ("post", "/items"),
        ("put", f"/items/{item_id}"),
        ("patch", f"/items/{item_id}"),
        ("delete", f"/items/{item_id}"),
        ("post", f"/items/{item_id}/publish"),
        ("post", f"/items/{item_id}/withdraw"),
        ("delete", f"/items/{item_id}/recipients/{recipient['id']}"),
    ):
        response = await getattr(api_client, method)(
            f"{PUBLIC}{path}", headers={TOKEN_HEADER: token}
        )
        # 405 onde a rota existe para outro método; 404 para caminhos
        # inexistentes. O invariante é: nenhuma capacidade de escrita e
        # nenhum estado alterado.
        assert response.status_code in (404, 405), (method, path, response.text)
    stored = await db_session.get(FamilyPortalItem, UUID(item_id))
    await db_session.refresh(stored)
    assert stored.status == "published" and stored.version == 1
