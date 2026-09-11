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
from unittest.mock import AsyncMock

import pytest

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
