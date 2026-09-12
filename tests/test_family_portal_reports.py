"""F14 onda 3 — relatórios pais F1 no portal da família (Tarefa 3.1, §3.5).

Cobre: candidato do picker restrito a entrega F1 ``standard``/``pais`` com
snapshot completo (legacy/expirada/revogada/incompleta com motivo, escolar e
outros tipos fora), publicação com metadados congelados (delivery/versão/hash),
detalhe público com o TEXTO DO SNAPSHOT (nunca o relatório atual nem o
rascunho), PDF real analisado com pypdf, revogação/expiração da entrega que
bloqueiam a leitura pública e o arquivo (409 neutro), snapshot incompleto
rejeitado ANTES de qualquer fallback, revalidação pós-render e não
redistribuição por colega. SQLite em memória.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.professional import Professional
from app.models.report_delivery import RECIPIENT_KIND_SCHOOL, ReportDelivery
from app.services import report_delivery_service
from app.services.report_export import REPORT_TYPE_LABELS

HEADER_TOKEN = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"
REPORT_CONTENT = (
    "## Sessão\nTexto congelado do snapshot entregue à família.\n\n"
    "## Conduta\nManter a rotina de exercícios em casa."
)
CONTENT_UNAVAILABLE = "Este conteúdo não está disponível no momento."


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _headers(token: str | None) -> dict[str, str]:
    return {} if token is None else {HEADER_TOKEN: token}


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: contador público sempre 'permite'."""
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


# --------------------------------------------------------------------------- #
# Helpers de portal (mesmos padrões dos focados da onda 2)
# --------------------------------------------------------------------------- #


def _base(patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _primary_caregiver(db_session, patient) -> Caregiver:
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    assert caregiver is not None
    return caregiver


async def _enable(api_client, headers, patient) -> dict:
    response = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _authorize(api_client, headers, patient, caregiver_id) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json={
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


async def _issue(api_client, headers, patient, recipient_id) -> tuple[str, dict]:
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


async def _ready_recipient(
    api_client, headers, patient, db_session
) -> tuple[str, dict]:
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    return token, recipient


async def _make_report(
    db_session,
    professional,
    patient,
    *,
    content: str = REPORT_CONTENT,
    report_type: str = "pais",
    status: str = "finalized",
) -> AIReport:
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type=report_type,
        date=date(2026, 9, 1),
        preview=content[:200],
        content=content,
        status=status,
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


async def _deliver(api_client, headers, report) -> dict:
    response = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=headers,
        json={"channel": "link"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_legacy_delivery(db_session, professional, patient, report) -> ReportDelivery:
    delivery = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot=None,
    )
    db_session.add(delivery)
    return delivery


def _seed_school_delivery(db_session, professional, patient, report) -> ReportDelivery:
    delivery = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_kind=RECIPIENT_KIND_SCHOOL,
        recipient_label="Escola Municipal · Coordenação",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        school_name="Escola Municipal",
        school_recipient_name="Coordenação",
        school_authorization={"reviewed": True},
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=report, patient=patient, professional=professional
        ),
    )
    db_session.add(delivery)
    return delivery


async def _create_item(
    api_client,
    headers,
    patient,
    *,
    delivery_id,
    title: str = "Relatório para casa",
    recipient_ids: list[str] | None = None,
):
    return await api_client.post(
        f"{_base(patient)}/items",
        headers=headers,
        json={
            "kind": "report",
            "source": {"deliveryId": str(delivery_id)},
            "content": {"title": title},
            "recipientIds": recipient_ids or [],
        },
    )


async def _publish(
    api_client,
    headers,
    patient,
    item_id,
    *,
    expected_version: int,
    fingerprint: str | None,
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


async def _publish_report_item(
    api_client, headers, patient, *, delivery_id, recipient_id: str, title: str = "Relatório para casa"
) -> dict:
    created = await _create_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery_id,
        title=title,
        recipient_ids=[recipient_id],
    )
    assert created.status_code == 201, created.text
    data = created.json()
    published = await _publish(
        api_client,
        headers,
        patient,
        data["id"],
        expected_version=data["version"],
        fingerprint=data["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text
    return published.json()


# --------------------------------------------------------------------------- #
# Candidatos do picker: só entrega standard/pais com snapshot completo
# --------------------------------------------------------------------------- #


async def test_report_candidates_only_standard_pais_with_fixed_snapshot(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    report = await _make_report(db_session, professional, patient)
    good = await _deliver(api_client, headers, report)

    legacy = _seed_legacy_delivery(db_session, professional, patient, report)
    revoked_row = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        revoked_at=datetime.now(UTC),
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=report, patient=patient, professional=professional
        ),
    )
    expired_row = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) - timedelta(days=1),
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=report, patient=patient, professional=professional
        ),
    )
    incomplete_row = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot={
            **report_delivery_service.build_delivery_snapshot(
                report=report, patient=patient, professional=professional
            ),
            "contentHash": "b" * 64,
        },
    )
    school = _seed_school_delivery(db_session, professional, patient, report)
    clinico_report = await _make_report(
        db_session, professional, patient, report_type="clinico"
    )
    clinico_delivery = ReportDelivery(
        report_id=clinico_report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=clinico_report, patient=patient, professional=professional
        ),
    )
    db_session.add_all(
        [legacy, revoked_row, expired_row, incomplete_row, clinico_delivery]
    )
    await db_session.commit()

    response = await api_client.get(
        f"{_base(patient)}/sources?kind=reportDelivery", headers=headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    by_id = {item["id"]: item for item in payload["items"]}

    eligible = by_id[good["id"]]
    assert set(eligible.keys()) == {
        "id",
        "kind",
        "label",
        "date",
        "sourceFingerprint",
        "eligible",
        "unavailableReason",
    }
    assert eligible["kind"] == "reportDelivery"
    assert eligible["label"] == (
        f"{REPORT_TYPE_LABELS['pais']} · {report.date.isoformat()}"
    )
    assert eligible["date"] == report.date.isoformat()
    assert eligible["eligible"] is True
    assert len(eligible["sourceFingerprint"]) == 64

    assert by_id[str(legacy.id)]["eligible"] is False
    assert "documento congelado" in by_id[str(legacy.id)]["unavailableReason"]
    assert by_id[str(revoked_row.id)]["eligible"] is False
    assert "revogada" in by_id[str(revoked_row.id)]["unavailableReason"]
    assert by_id[str(expired_row.id)]["eligible"] is False
    assert "expirada" in by_id[str(expired_row.id)]["unavailableReason"]
    assert by_id[str(incomplete_row.id)]["eligible"] is False
    assert "incompleto" in by_id[str(incomplete_row.id)]["unavailableReason"]

    # Escolares e relatórios de outro tipo NÃO são listados.
    assert str(school.id) not in by_id
    assert str(clinico_delivery.id) not in by_id

    # Busca por data (ISO) filtra os candidatos; sem correspondência, vazio.
    filtered = await api_client.get(
        f"{_base(patient)}/sources?kind=reportDelivery&q=2026-09", headers=headers
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == len(by_id)
    empty = await api_client.get(
        f"{_base(patient)}/sources?kind=reportDelivery&q=1999", headers=headers
    )
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0

    # O relatório atual não entra no candidato (fingerprint é da entrega).
    assert eligible["sourceFingerprint"] != _sha256(REPORT_CONTENT)


async def test_report_create_rejects_vedados_and_foreign_sources(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    report = await _make_report(db_session, professional, patient)

    # Entrega escolar → 422 explícito, sem stub.
    school = _seed_school_delivery(db_session, professional, patient, report)
    await db_session.commit()
    school_item = await _create_item(
        api_client, headers, patient, delivery_id=school.id
    )
    assert school_item.status_code == 422, school_item.text
    assert "escolar" in school_item.json()["detail"].lower()

    # Relatório de outro tipo → 422 explícito.
    clinico = await _make_report(db_session, professional, patient, report_type="clinico")
    clinico_delivery = ReportDelivery(
        report_id=clinico.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=clinico, patient=patient, professional=professional
        ),
    )
    db_session.add(clinico_delivery)
    await db_session.commit()
    clinico_item = await _create_item(
        api_client, headers, patient, delivery_id=clinico_delivery.id
    )
    assert clinico_item.status_code == 422
    assert "relatório para pais" in clinico_item.json()["detail"].lower()

    # Entrega de outro paciente/dono → 404.
    other = Professional(
        email=f"outro-{uuid.uuid4().hex}@example.com",
        password_hash=hash_password("x"),
        name="Outro",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    other_report = AIReport(
        professional_id=other.id,
        patient_id=patient.id,
        type="pais",
        date=date(2026, 9, 1),
        preview="x",
        content=REPORT_CONTENT,
        status="finalized",
    )
    db_session.add(other_report)
    await db_session.commit()
    foreign = ReportDelivery(
        report_id=other_report.id,
        professional_id=other.id,
        patient_id=patient.id,
        channel="link",
        recipient_label="Link avulso",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        document_snapshot=report_delivery_service.build_delivery_snapshot(
            report=other_report, patient=patient, professional=other
        ),
    )
    db_session.add(foreign)
    await db_session.commit()
    foreign_item = await _create_item(
        api_client, headers, patient, delivery_id=foreign.id
    )
    assert foreign_item.status_code == 404


# --------------------------------------------------------------------------- #
# Publicação: snapshot congelado, detalhe e PDF real
# --------------------------------------------------------------------------- #


async def test_report_publish_freezes_snapshot_and_serves_detail_and_pdf(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    delivery = await _deliver(api_client, headers, report)
    assert delivery["snapshotMode"] == "fixed"
    assert delivery["reportVersion"] == 1
    assert delivery["contentHash"] == _sha256(REPORT_CONTENT)

    created = await _create_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery["id"],
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["source"] == {"deliveryId": delivery["id"]}
    assert draft["draftContent"] == {"title": "Relatório para casa"}
    assert draft["sourceChanged"] is False

    published = await _publish(
        api_client,
        headers,
        patient,
        draft["id"],
        expected_version=1,
        fingerprint=draft["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    revisions = await api_client.get(
        f"{_base(patient)}/items/{draft['id']}/revisions", headers=headers
    )
    metadata = revisions.json()["items"][0]["sourceMetadata"]
    assert metadata["deliveryId"] == delivery["id"]
    assert metadata["reportVersion"] == 1
    assert metadata["contentHash"] == _sha256(REPORT_CONTENT)
    assert metadata["reportType"] == "pais"
    # O texto NÃO é copiado para a revisão F14 (vive na entrega F1).
    assert "content" not in metadata
    assert REPORT_CONTENT not in str(revisions.json())

    listed = await api_client.get(
        f"{PUBLIC}/items?kind=report", headers=_headers(token)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["available"] is True

    detail = await api_client.get(
        f"{PUBLIC}/items/{draft['id']}", headers=_headers(token)
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "content",
        "reportDate",
        "reportVersion",
        "contentHash",
        "patientName",
        "professionalName",
        "professionalCouncil",
    }
    assert body["content"] == REPORT_CONTENT
    assert body["reportDate"] == report.date.isoformat()
    assert body["reportVersion"] == 1
    assert body["contentHash"] == _sha256(REPORT_CONTENT)
    assert body["patientName"] == patient.name
    assert body["professionalName"] == professional.name
    assert body["professionalCouncil"] == "CREFITO"

    # PDF real: renderer F1 + identidade F2; nome seguro sem a criança.
    file_response = await api_client.get(
        f"{PUBLIC}/items/{draft['id']}/file", headers=_headers(token)
    )
    assert file_response.status_code == 200, file_response.text
    assert file_response.headers["content-type"] == "application/pdf"
    assert (
        f'relatorio-familiar-{draft["id"]}.pdf'
        in file_response.headers["content-disposition"]
    )
    assert patient.name.split(" ")[0] not in file_response.headers[
        "content-disposition"
    ]
    pdf = PdfReader(BytesIO(file_response.content))
    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "congelado" in text
    assert professional.name in text

    # Corrigir o relatório NÃO altera o snapshot publicado.
    edited = await api_client.patch(
        f"/api/v1/ai/reports/{report.id}",
        headers=headers,
        json={"content": "## Sessão\nTexto NOVO depois da entrega."},
    )
    assert edited.status_code == 200, edited.text
    frozen = await api_client.get(
        f"{PUBLIC}/items/{draft['id']}", headers=_headers(token)
    )
    assert frozen.json()["content"] == REPORT_CONTENT
    assert frozen.json()["contentHash"] == _sha256(REPORT_CONTENT)
    frozen_file = await api_client.get(
        f"{PUBLIC}/items/{draft['id']}/file", headers=_headers(token)
    )
    frozen_pdf = PdfReader(BytesIO(frozen_file.content))
    frozen_text = "\n".join(page.extract_text() or "" for page in frozen_pdf.pages)
    assert "Texto NOVO" not in frozen_text

    # Repetir a publicação sem mudança é no-op (sem nova revisão).
    private = await api_client.get(
        f"{_base(patient)}/items/{draft['id']}", headers=headers
    )
    noop = await _publish(
        api_client,
        headers,
        patient,
        draft["id"],
        expected_version=1,
        fingerprint=private.json()["sourceFingerprint"],
    )
    assert noop.status_code == 200, noop.text
    revisions_after = await api_client.get(
        f"{_base(patient)}/items/{draft['id']}/revisions", headers=headers
    )
    assert len(revisions_after.json()["items"]) == 1


async def test_report_new_delivery_distributes_correction_and_old_stays_frozen(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    first_delivery = await _deliver(api_client, headers, report)
    first_item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=first_delivery["id"],
        recipient_id=recipient["id"],
    )

    # Nova correção + nova entrega + nova publicação: coexistência dos fixes.
    corrected = "## Sessão\nTexto corrigido em nova entrega."
    patched = await api_client.patch(
        f"/api/v1/ai/reports/{report.id}",
        headers=headers,
        json={"content": corrected},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2
    second_delivery = await _deliver(api_client, headers, report)
    assert second_delivery["reportVersion"] == 2
    second_item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=second_delivery["id"],
        recipient_id=recipient["id"],
        title="Relatório corrigido",
    )

    first_detail = await api_client.get(
        f"{PUBLIC}/items/{first_item['id']}", headers=_headers(token)
    )
    second_detail = await api_client.get(
        f"{PUBLIC}/items/{second_item['id']}", headers=_headers(token)
    )
    assert first_detail.json()["content"] == REPORT_CONTENT
    assert second_detail.json()["content"] == corrected

    # Revogar a primeira entrega bloqueia só a primeira publicação.
    revoked = await api_client.delete(
        f"/api/v1/ai/reports/{report.id}/deliveries/{first_delivery['id']}",
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    first_after = await api_client.get(
        f"{PUBLIC}/items/{first_item['id']}", headers=_headers(token)
    )
    assert first_after.json()["available"] is False
    first_file = await api_client.get(
        f"{PUBLIC}/items/{first_item['id']}/file", headers=_headers(token)
    )
    assert first_file.status_code == 409
    assert first_file.json()["detail"] == CONTENT_UNAVAILABLE
    second_still = await api_client.get(
        f"{PUBLIC}/items/{second_item['id']}/file", headers=_headers(token)
    )
    assert second_still.status_code == 200


# --------------------------------------------------------------------------- #
# Snapshot incompleto/legacy: rejeitado ANTES de qualquer fallback
# --------------------------------------------------------------------------- #


async def test_report_legacy_and_incomplete_snapshots_never_fall_back(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)

    # Entrega legacy (sem snapshot) não vira rascunho: 422 antes do fallback.
    legacy = _seed_legacy_delivery(db_session, professional, patient, report)
    await db_session.commit()
    blocked = await _create_item(
        api_client, headers, patient, delivery_id=legacy.id
    )
    assert blocked.status_code == 422, blocked.text
    assert "documento congelado" in blocked.json()["detail"]

    # Rascunho criado com snapshot válido; a entrega vira legacy/incompleta
    # DEPOIS → publicar é 409 e a leitura pública fica indisponível (jamais
    # serve o texto atual do relatório).
    delivery = await _deliver(api_client, headers, report)
    item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery["id"],
        recipient_id=recipient["id"],
    )

    row = await db_session.get(ReportDelivery, uuid.UUID(delivery["id"]))
    assert row is not None
    original_snapshot = dict(row.document_snapshot)
    row.document_snapshot = None
    await db_session.commit()

    detail = await api_client.get(
        f"{PUBLIC}/items/{item['id']}", headers=_headers(token)
    )
    assert detail.status_code == 200
    assert detail.json()["available"] is False
    assert detail.json()["unavailableReason"] == CONTENT_UNAVAILABLE
    assert REPORT_CONTENT not in detail.text
    file_blocked = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(token)
    )
    assert file_blocked.status_code == 409
    assert file_blocked.json()["detail"] == CONTENT_UNAVAILABLE

    # Snapshot presente mas com hash divergente também falha fechado.
    row.document_snapshot = {**original_snapshot, "contentHash": "b" * 64}
    await db_session.commit()
    incomplete = await api_client.get(
        f"{PUBLIC}/items/{item['id']}", headers=_headers(token)
    )
    assert incomplete.json()["available"] is False
    incomplete_file = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(token)
    )
    assert incomplete_file.status_code == 409

    # Novo rascunho com a entrega degradada → 422 (nunca rascunho inválido).
    again = await _create_item(
        api_client, headers, patient, delivery_id=delivery["id"]
    )
    assert again.status_code == 422
    assert "incompleto" in again.json()["detail"]


# --------------------------------------------------------------------------- #
# Revogação/expiração e retirada
# --------------------------------------------------------------------------- #


async def test_report_publish_revalidation_and_withdraw(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    delivery = await _deliver(api_client, headers, report)

    # Rascunho com a entrega vigente; ela é revogada antes da publicação → 409.
    created = await _create_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery["id"],
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    revoked = await api_client.delete(
        f"/api/v1/ai/reports/{report.id}/deliveries/{delivery['id']}",
        headers=headers,
    )
    assert revoked.status_code == 200
    stale = await _publish(
        api_client,
        headers,
        patient,
        draft["id"],
        expected_version=1,
        fingerprint=draft["sourceFingerprint"],
    )
    assert stale.status_code == 409, stale.text
    assert "revogada" in stale.json()["detail"]

    # Válida de novo? Não — entrega revogada não volta; criar outra entrega.
    fresh_delivery = await _deliver(api_client, headers, report)
    recreated = await _create_item(
        api_client,
        headers,
        patient,
        delivery_id=fresh_delivery["id"],
        recipient_ids=[recipient["id"]],
    )
    assert recreated.status_code == 201, recreated.text
    item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=fresh_delivery["id"],
        recipient_id=recipient["id"],
    )

    # Retirada do item: 404 no detalhe e no arquivo; idempotente.
    withdrawn = await api_client.post(
        f"{_base(patient)}/items/{item['id']}/withdraw", headers=headers, json={}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    again = await api_client.post(
        f"{_base(patient)}/items/{item['id']}/withdraw", headers=headers, json={}
    )
    assert again.status_code == 200
    gone = await api_client.get(
        f"{PUBLIC}/items/{item['id']}", headers=_headers(token)
    )
    assert gone.status_code == 404
    gone_file = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(token)
    )
    assert gone_file.status_code == 404
    listed = await api_client.get(
        f"{PUBLIC}/items?kind=report", headers=_headers(token)
    )
    assert listed.json()["items"] == []


async def test_report_expired_delivery_blocks_public_read(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=(await _deliver(api_client, headers, report))["id"],
        recipient_id=recipient["id"],
    )

    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.patient_id == patient.id)
    )
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    detail = await api_client.get(
        f"{PUBLIC}/items/{item['id']}", headers=_headers(token)
    )
    assert detail.json()["available"] is False
    file_blocked = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(token)
    )
    assert file_blocked.status_code == 409


# --------------------------------------------------------------------------- #
# Revalidação pós-render e escopo do dono
# --------------------------------------------------------------------------- #


async def test_report_revalidates_after_render(
    api_client, auth_headers, db_session, patient, professional, monkeypatch
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    delivery = await _deliver(api_client, headers, report)
    item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery["id"],
        recipient_id=recipient["id"],
    )

    import app.services.family_portal_files as files_module

    async def render_with_revocation(fn, *args, **kwargs):
        row = await db_session.get(ReportDelivery, uuid.UUID(delivery["id"]))
        row.revoked_at = datetime.now(UTC)
        await db_session.commit()
        return fn(*args, **kwargs)

    monkeypatch.setattr(files_module, "run_in_threadpool", render_with_revocation)
    blocked = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(token)
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == CONTENT_UNAVAILABLE


async def test_report_is_owner_scoped_and_shared_does_not_redistribute(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    report = await _make_report(db_session, professional, patient)
    delivery = await _deliver(api_client, headers, report)
    item = await _publish_report_item(
        api_client,
        headers,
        patient,
        delivery_id=delivery["id"],
        recipient_id=recipient["id"],
    )

    other = Professional(
        email=f"colega-{uuid.uuid4().hex}@example.com",
        password_hash=hash_password("x"),
        name="Colega",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    # Colega não lista as fontes de relatório do dono nem copia a entrega.
    sources = await api_client.get(
        f"{_base(patient)}/sources?kind=reportDelivery", headers=other_headers
    )
    assert sources.status_code == 404
    copied = await _create_item(
        api_client, other_headers, patient, delivery_id=delivery["id"]
    )
    assert copied.status_code == 404
    detail = await api_client.get(
        f"{_base(patient)}/items/{item['id']}", headers=other_headers
    )
    assert detail.status_code == 404
    preview = await api_client.get(
        f"{_base(patient)}/preview?recipientId={recipient['id']}&kind=report",
        headers=other_headers,
    )
    assert preview.status_code == 404
