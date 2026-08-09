from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.assessment import Assessment
from app.models.patient import Patient


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


def _safe_answers() -> dict[str, str]:
    return {
        str(item_id): "nao" if item_id in {2, 5, 12} else "sim"
        for item_id in range(1, 21)
    }


def _completed_payload(*, acknowledge_age: bool = False) -> dict:
    answers = _safe_answers()
    return {
        "result": "0 itens de risco — baixo risco.",
        "percentage": 100,
        "interpretation": "Total de respostas de risco no M-CHAT-R: 0/20",
        "fields": [],
        "answers": answers,
        "scores": {
            "summary": "0 itens de risco — baixo risco.",
            "detail": "Total de respostas de risco no M-CHAT-R: 0/20",
            "engine": "client",
            "stage1Failed": 0,
            "stage1Level": "baixo",
        },
        "informant": "Mãe",
        "metadata": {
            "mchatStage": "screening_only",
            "mchatStage1Failed": 0,
            "mchatFollowUpOutcomes": {},
            "mchatAgeOutsideStandardAcknowledged": acknowledge_age,
        },
    }


async def _patient_at_months(db_session, professional, months: int) -> Patient:
    patient = Patient(
        professional_id=professional.id,
        name=f"Paciente {months} meses",
        birth_date=date.today() - timedelta(days=months * 30),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


async def test_upsert_e_retomada_reutilizam_um_unico_rascunho_mchat(
    api_client, db_session, professional, auth_headers
):
    patient = await _patient_at_months(db_session, professional, 20)
    url = f"/api/v1/patients/{patient.id}/assessments/drafts/mchat"

    first = await api_client.put(
        url,
        headers=auth_headers,
        json={
            "answers": {"1": "sim"},
            "informant": "Mãe",
            "metadata": {"mchatDraft": {"stage": "screening", "followUpIndex": 0}},
        },
    )
    await db_session.commit()
    second = await api_client.put(
        url,
        headers=auth_headers,
        json={
            "answers": {"1": "sim", "2": "nao"},
            "informant": "Mãe",
            "metadata": {"mchatDraft": {"stage": "screening", "followUpIndex": 0}},
        },
    )
    await db_session.commit()
    resumed = await api_client.get(url, headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert resumed.status_code == 200
    assert first.json()["id"] == second.json()["id"] == resumed.json()["id"]
    assert resumed.json()["answers"] == {"1": "sim", "2": "nao"}
    assert resumed.json()["metadata"]["mchatDraft"]["stage"] == "screening"
    rows = (
        await db_session.execute(
            select(Assessment).where(
                Assessment.patient_id == patient.id,
                Assessment.protocol_id == "mchat",
                Assessment.status == "draft",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_conclusao_atualiza_o_rascunho_em_vez_de_criar_duplicata(
    api_client, db_session, professional, auth_headers
):
    patient = await _patient_at_months(db_session, professional, 20)
    url = f"/api/v1/patients/{patient.id}/assessments/drafts/mchat"
    draft = await api_client.put(
        url,
        headers=auth_headers,
        json={"answers": {"1": "sim"}, "informant": "Mãe", "metadata": {}},
    )
    await db_session.commit()

    completed = await api_client.post(
        f"{url}/complete",
        headers=auth_headers,
        json=_completed_payload(),
    )

    assert completed.status_code == 200
    assert completed.json()["id"] == draft.json()["id"]
    assert completed.json()["status"] == "completed"
    assert completed.json()["metadata"]["mchatStage"] == "screening_only"
    rows = (
        await db_session.execute(
            select(Assessment).where(
                Assessment.patient_id == patient.id,
                Assessment.protocol_id == "mchat",
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_mchat_rejeita_informante_numerico_e_respostas_incompletas(
    api_client, db_session, professional, auth_headers
):
    patient = await _patient_at_months(db_session, professional, 20)
    url = f"/api/v1/patients/{patient.id}/assessments/drafts/mchat/complete"
    numeric = _completed_payload()
    numeric["informant"] = "44545"
    incomplete = _completed_payload()
    incomplete["answers"].pop("20")

    numeric_response = await api_client.post(url, headers=auth_headers, json=numeric)
    incomplete_response = await api_client.post(url, headers=auth_headers, json=incomplete)

    assert numeric_response.status_code == 400
    assert "informante" in numeric_response.json()["detail"].lower()
    assert incomplete_response.status_code == 400
    assert "20 itens" in incomplete_response.json()["detail"]


async def test_mchat_rejeita_score_incoerente_com_as_respostas(
    api_client, db_session, professional, auth_headers
):
    patient = await _patient_at_months(db_session, professional, 20)
    payload = _completed_payload()
    payload["scores"]["stage1Failed"] = 4
    payload["scores"]["stage1Level"] = "medio"

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments/drafts/mchat/complete",
        headers=auth_headers,
        json=payload,
    )

    assert response.status_code == 400
    assert "pontuação" in response.json()["detail"].lower()


async def test_mchat_fora_de_16_a_30_meses_exige_ciencia_do_profissional(
    api_client, db_session, professional, auth_headers
):
    patient = await _patient_at_months(db_session, professional, 36)
    url = f"/api/v1/patients/{patient.id}/assessments/drafts/mchat/complete"

    blocked = await api_client.post(url, headers=auth_headers, json=_completed_payload())
    allowed = await api_client.post(
        url,
        headers=auth_headers,
        json=_completed_payload(acknowledge_age=True),
    )

    assert blocked.status_code == 400
    assert "16 a 30 meses" in blocked.json()["detail"]
    assert allowed.status_code == 200
    assert allowed.json()["metadata"]["mchatAgeOutsideStandardAcknowledged"] is True
