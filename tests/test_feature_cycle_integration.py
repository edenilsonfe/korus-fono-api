"""Fechamento do ciclo F3/F20/F6/F16/F17 — integrações REAIS entre features.

Duas cadeias, sem fabricar contrato novo:

1. **F4 × F3** — a comparação de reavaliações (40→68, delta 28) alimenta o laudo
   consolidado: a composição registrada no laudo traz a MESMA comparação (base,
   alvo e delta) que o endpoint público de comparação devolve à profissional.
2. **F3 × F1 × F2 (entrega)** — laudo consolidado finalizado é entregue por link
   com snapshot fixo; corrigir o laudo DEPOIS não muda o link antigo (versão e
   hash continuam os da entrega) e o export público serve o texto congelado.

SQLite em memória; o provedor de IA é mockado (padrão de
``tests/test_report_composition.py``).
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense

LLM_DRAFT = (
    "## Síntese\nO paciente demonstra avanço consolidado entre as reavaliações.\n\n"
    "## Conduta\nManter terapia semanal e reavaliar em três meses."
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value=LLM_DRAFT)
    monkeypatch.setattr("app.services.report_composition_service.run_llm", mock)
    return mock


async def _create_completed_assessment(
    api_client, auth_headers, patient, protocol_id: str, day: str, percentage: int
):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=auth_headers,
        json={
            "protocolId": protocol_id,
            "date": day,
            "result": "Atraso leve",
            "percentage": percentage,
            "informant": "Mãe",
            "scores": {
                "domains": {"linguagem": percentage // 10, "motor": 5},
                "total": 15,
                "summary": "Resumo sintético",
            },
            "answers": {"1": "sim", "2": "nao"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_consolidated(api_client, auth_headers, patient, composition: dict):
    response = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "consolidado",
            "composition": composition,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _patch(api_client, auth_headers, report_id: str, payload: dict):
    return await api_client.patch(
        f"/api/v1/ai/reports/{report_id}", headers=auth_headers, json=payload
    )


async def test_reassessment_comparison_feeds_consolidated_report(
    api_client, auth_headers, db_session, patient, professional, llm
):
    old = await _create_completed_assessment(
        api_client, auth_headers, patient, "portage", "2026-01-10", 40
    )
    new = await _create_completed_assessment(
        api_client, auth_headers, patient, "portage", "2026-06-10", 68
    )
    other = await _create_completed_assessment(
        api_client, auth_headers, patient, "vanderbilt", "2026-02-01", 55
    )

    # F4: o endpoint de comparação do paciente devolve o delta real (28).
    compare = await api_client.get(
        f"/api/v1/patients/{patient.id}/assessments/compare",
        headers=auth_headers,
        params={"baseId": old["id"], "targetId": new["id"]},
    )
    assert compare.status_code == 200, compare.text
    assert compare.json()["percentageDelta"] == 28

    # F3: o laudo consolidado seleciona as duas aplicações + a comparação, e a
    # composição registrada reusa o MESMO serviço de comparação (delta idêntico).
    data = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [old["id"], new["id"], other["id"]],
            "comparisons": [{"baseId": old["id"], "targetId": new["id"]}],
        },
    )
    composition = await api_client.get(
        f"/api/v1/ai/reports/{data['id']}/composition", headers=auth_headers
    )
    assert composition.status_code == 200, composition.text
    body = composition.json()

    kinds = {(item["kind"], item["id"]) for item in body["sources"]}
    assert ("assessment", old["id"]) in kinds
    assert ("assessment", new["id"]) in kinds

    assert len(body["comparisons"]) == 1
    recorded = body["comparisons"][0]
    assert recorded["baseId"] == old["id"]
    assert recorded["targetId"] == new["id"]
    assert recorded["percentageDelta"] == compare.json()["percentageDelta"] == 28

    # Seção determinística de instrumentos entrou no texto gerado.
    assert "Portage" in data["content"]
    assert "Vanderbilt" in data["content"]


async def test_consolidated_delivery_snapshot_survives_later_edit(
    api_client, auth_headers, db_session, patient, professional, llm
):
    old = await _create_completed_assessment(
        api_client, auth_headers, patient, "portage", "2026-01-10", 40
    )
    other = await _create_completed_assessment(
        api_client, auth_headers, patient, "vanderbilt", "2026-02-01", 55
    )
    data = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [old["id"], other["id"]]},
    )

    # Finaliza o rascunho (controle otimista obrigatório no consolidado).
    finalize = await _patch(
        api_client,
        auth_headers,
        data["id"],
        {"content": data["content"], "status": "finalized", "expectedVersion": 1},
    )
    assert finalize.status_code == 200, finalize.text
    assert finalize.json()["version"] == 2

    # Entrega por link: o snapshot fixa o texto e a versão daquele momento.
    delivery = await api_client.post(
        f"/api/v1/ai/reports/{data['id']}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert delivery.status_code == 201, delivery.text
    delivered = delivery.json()
    assert delivered["reportVersion"] == 2
    assert delivered["snapshotMode"] == "fixed"
    assert delivered["contentHash"] == _sha256(data["content"])
    token = delivered["url"].rsplit("/", 1)[-1]

    # Corrige o laudo DEPOIS da entrega (vira versão 3).
    revised = data["content"] + "\n\nRevisão posterior aprovada."
    edit = await _patch(
        api_client,
        auth_headers,
        data["id"],
        {"content": revised, "status": "finalized", "expectedVersion": 2},
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["version"] == 3

    # O link antigo continua servindo a versão entregue — intacta.
    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    served = public.json()
    assert served["content"] == data["content"]
    assert served["reportVersion"] == 2
    assert served["contentHash"] == _sha256(data["content"])
    assert served["snapshotMode"] == "fixed"
    assert "Revisão posterior aprovada." not in served["content"]

    # O export público do link antigo também sai do snapshot, não do texto vivo.
    exported = await api_client.get(
        f"/api/v1/report-deliveries/{token}/export", params={"format": "txt"}
    )
    assert exported.status_code == 200, exported.text
    text = exported.content.decode("utf-8")
    assert "Revisão posterior aprovada." not in text
    assert LLM_DRAFT.splitlines()[1] in text


# --------------------------------------------------------------------------- #
# F14 × F1 × F17 × F16 — chains do portal da família (onda F14)
# --------------------------------------------------------------------------- #


async def _family_caregiver(db_session, patient) -> Caregiver:
    caregiver = Caregiver(
        patient_id=patient.id, name="Mãe Sintética", relation="Mãe", is_primary=True
    )
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


async def _enable_family_portal(api_client, headers, patient) -> None:
    response = await api_client.put(
        f"/api/v1/patients/{patient.id}/family-portal",
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text


async def _authorize_family_recipient(api_client, headers, patient, caregiver_id) -> dict:
    response = await api_client.put(
        f"/api/v1/patients/{patient.id}/family-portal/recipients/{caregiver_id}",
        headers=headers,
        json={
            "appointmentsEnabled": False,
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reference": "Termo sintético do ciclo",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue_family_grant(api_client, headers, patient, recipient_id) -> str:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/family-portal/recipients/{recipient_id}/grants",
        headers=headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": 1,
            "rotateFromGrantId": None,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["url"].split("#token=", 1)[1]


def _family_headers(token: str) -> dict[str, str]:
    return {"X-Family-Portal-Token": token}


async def _portal_fixture(api_client, auth_headers, db_session, patient) -> tuple[dict, str]:
    caregiver = await _family_caregiver(db_session, patient)
    await _enable_family_portal(api_client, auth_headers, patient)
    recipient = await _authorize_family_recipient(
        api_client, auth_headers, patient, caregiver.id
    )
    token = await _issue_family_grant(api_client, auth_headers, patient, recipient["id"])
    return recipient, token


async def test_f14_report_item_serves_frozen_snapshot_and_revocation_blocks_it(
    api_client, auth_headers, db_session, patient, professional
):
    # F1: relatório "pais" finalizado (seed local, mesmo padrão de
    # tests/test_report_deliveries.py) + entrega por link com snapshot fixo.
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="pais",
        date=date(2026, 9, 1),
        preview="Resumo do relatório",
        content="## Como está o(a) paciente\nJoão evoluiu bem.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)

    delivery = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert delivery.status_code == 201, delivery.text
    delivery_id = delivery.json()["id"]
    assert delivery.json()["snapshotMode"] == "fixed"
    frozen_text = report.content

    recipient, token = await _portal_fixture(
        api_client, auth_headers, db_session, patient
    )

    # Candidato real (entrega standard de relatório pais) + criação/publicação.
    sources = await api_client.get(
        f"/api/v1/patients/{patient.id}/family-portal/sources",
        headers=auth_headers,
        params={"kind": "reportDelivery"},
    )
    assert sources.status_code == 200, sources.text
    candidate = next(
        item for item in sources.json()["items"] if item["id"] == delivery_id
    )
    assert candidate["eligible"] is True

    created_item = await api_client.post(
        f"/api/v1/patients/{patient.id}/family-portal/items",
        headers=auth_headers,
        json={
            "kind": "report",
            "source": {"deliveryId": delivery_id},
            "content": {"title": "Relatório para os pais"},
            "recipientIds": [recipient["id"]],
        },
    )
    assert created_item.status_code == 201, created_item.text
    item_id = created_item.json()["id"]
    published = await api_client.post(
        f"/api/v1/patients/{patient.id}/family-portal/items/{item_id}/publish",
        headers=auth_headers,
        json={
            "expectedVersion": created_item.json()["version"],
            "expectedSourceFingerprint": candidate["sourceFingerprint"],
            "reviewed": True,
        },
    )
    assert published.status_code == 200, published.text

    # A família lê o TEXTO DO SNAPSHOT congelado (não o relatório vivo).
    detail = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}", headers=_family_headers(token)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["available"] is True
    assert detail.json()["content"] == frozen_text

    # Editar o relatório DEPOIS da entrega não muda o item F14.
    report.content = frozen_text + "\n\nAdendo posterior."
    db_session.add(report)
    await db_session.commit()
    still = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}", headers=_family_headers(token)
    )
    assert still.json()["content"] == frozen_text
    assert "Adendo posterior." not in still.json()["content"]

    # Revogar a entrega F1 bloqueia o item (motivo genérico) e o arquivo.
    revoked = await api_client.delete(
        f"/api/v1/ai/reports/{report.id}/deliveries/{delivery_id}",
        headers=auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revokedAt"] is not None
    blocked = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}", headers=_family_headers(token)
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["available"] is False
    assert (
        blocked.json()["unavailableReason"]
        == "Este conteúdo não está disponível no momento."
    )
    file_blocked = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}/file", headers=_family_headers(token)
    )
    assert file_blocked.status_code == 409


@pytest.fixture
def material_storage(monkeypatch):
    payload = b"%PDF-1.4 material sintetico do ciclo F14"
    sha = hashlib.sha256(payload).hexdigest()

    async def fake_download_limited(key, max_bytes=None, timeout_seconds=None):
        return payload, "application/pdf"

    monkeypatch.setattr(
        "app.services.family_portal_files.storage_service.download_limited",
        fake_download_limited,
    )
    return SimpleNamespace(payload=payload, sha=sha)


async def _seed_licensed_material(
    db_session, professional, *, sha: str
) -> tuple[Resource, ResourceLicense]:
    resource = Resource(
        owner_professional_id=professional.id,
        title="Cartões de animais",
        description="",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=100,
        author=professional.name,
        storage_key=f"resources/test/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
        content_sha256=sha,
    )
    db_session.add(resource)
    await db_session.flush()
    license_row = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status="declared",
        origin="original",
        rights_holder=professional.name,
        attribution="Uso autorizado pela autora",
        allow_professional_distribution=True,
        allow_family_delivery=True,
        content_sha256=sha,
    )
    db_session.add(license_row)
    await db_session.commit()
    await db_session.refresh(resource)
    await db_session.refresh(license_row)
    return resource, license_row


async def test_f14_material_item_freezes_license_and_reference_guard_blocks_delete(
    api_client, auth_headers, db_session, patient, professional, material_storage
):
    resource, license_row = await _seed_licensed_material(
        db_session, professional, sha=material_storage.sha
    )
    recipient, token = await _portal_fixture(
        api_client, auth_headers, db_session, patient
    )

    sources = await api_client.get(
        f"/api/v1/patients/{patient.id}/family-portal/sources",
        headers=auth_headers,
        params={"kind": "resource"},
    )
    assert sources.status_code == 200, sources.text
    candidate = next(
        item for item in sources.json()["items"] if item["id"] == str(resource.id)
    )
    assert candidate["eligible"] is True

    created_item = await api_client.post(
        f"/api/v1/patients/{patient.id}/family-portal/items",
        headers=auth_headers,
        json={
            "kind": "material",
            "source": {"resourceId": str(resource.id)},
            "content": {"title": "Cartões de animais", "instructions": "Recorte e use na mesa."},
            "recipientIds": [recipient["id"]],
        },
    )
    assert created_item.status_code == 201, created_item.text
    item_id = created_item.json()["id"]
    published = await api_client.post(
        f"/api/v1/patients/{patient.id}/family-portal/items/{item_id}/publish",
        headers=auth_headers,
        json={
            "expectedVersion": created_item.json()["version"],
            "expectedSourceFingerprint": candidate["sourceFingerprint"],
            "reviewed": True,
        },
    )
    assert published.status_code == 200, published.text

    # Detalhe público traz metadados congelados (nunca bytes no JSON).
    detail = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}", headers=_family_headers(token)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["available"] is True
    assert detail.json()["instructions"] == "Recorte e use na mesa."
    assert detail.json()["attribution"] == "Uso autorizado pela autora"

    # Arquivo autorizado serve os bytes reais com nome próprio.
    served = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}/file", headers=_family_headers(token)
    )
    assert served.status_code == 200, served.text
    assert served.content == material_storage.payload
    assert f"material-{item_id}" in served.headers.get("content-disposition", "")

    # F17 × F14: o item F14 congela a referência — excluir o recurso é 409.
    blocked_delete = await api_client.delete(
        f"/api/v1/resources/{resource.id}", headers=auth_headers
    )
    assert blocked_delete.status_code == 409, blocked_delete.text

    # Licença revogada depois → família perde o conteúdo e o arquivo.
    license_row.status = "revoked"
    db_session.add(license_row)
    await db_session.commit()
    flipped = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}", headers=_family_headers(token)
    )
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["available"] is False
    file_after = await api_client.get(
        f"/api/v1/family-portal/items/{item_id}/file", headers=_family_headers(token)
    )
    assert file_after.status_code == 409


async def test_f14_and_f16_public_surfaces_do_not_share_tokens(
    api_client, auth_headers, db_session, patient
):
    """Independência F14×F16: cada superfície pública valida SÓ o seu próprio
    header. O token do portal não abre o programa de casa (410 no padrão do
    F16) e um header alheio é ignorado pela superfície do F14."""
    _recipient, token = await _portal_fixture(
        api_client, auth_headers, db_session, patient
    )

    cross = await api_client.get(
        "/api/v1/home-program-responses", headers={"X-Home-Program-Token": token}
    )
    assert cross.status_code == 410

    wrong_header = await api_client.get(
        "/api/v1/family-portal", headers={"X-Home-Program-Token": "qualquer"}
    )
    assert wrong_header.status_code == 410

    ok = await api_client.get(
        "/api/v1/family-portal", headers=_family_headers(token)
    )
    assert ok.status_code == 200, ok.text
