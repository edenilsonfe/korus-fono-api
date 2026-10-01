import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.constants.ai_flags import AI_ASSESSMENT_GOALS_FLAG
from app.core.config import get_settings
from app.core.security import hash_password
from app.models.ai import AIJob
from app.models.assessment import Assessment
from app.models.battery import BatterySubformAssessment
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.ai_workflow_service import SuggestedGoals

URL = "/api/v1/ai/assessment-goals"
VALID = (
    '```json\n{"goals": ['
    '{"title": "Produzir /r/ em início de palavra", "area": "Fonologia", "rationale": "PCC 61% na imitação"},'
    '{"title": "Nomear 20 figuras do cotidiano", "area": "Vocabulário", "rationale": "Vocabulário abaixo do esperado"}'
    "]}\n```"
)


async def _flag(db_session, enabled=True):
    db_session.add(FeatureFlag(key=AI_ASSESSMENT_GOALS_FLAG, description="t", enabled_global=enabled))
    await db_session.commit()


async def _assessment(db_session, patient, professional):
    assessment = Assessment(
        patient_id=patient.id, professional_id=professional.id, protocol_id="abfw",
        date=date(2026, 9, 20), result="Alteração fonológica", percentage=58,
        interpretation="Processos de simplificação",
        scores={"domains": {"fono": {"title": "Fonologia", "percentage": 58}}},
    )
    db_session.add(assessment)
    await db_session.flush()
    db_session.add(BatterySubformAssessment(
        battery_id=assessment.id, instrument_slug="abfw", subform_slug="fonologia-imitacao",
        status="completed", scores={"domains": {"pcc": {"title": "PCC", "percentage": 61}}},
    ))
    await db_session.commit()
    return assessment


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value=VALID)
    monkeypatch.setattr("app.services.ai_workflow_service.run_llm", mock)
    return mock


async def test_flag_disabled_returns_403(api_client, auth_headers, patient, professional, db_session, llm):
    await _flag(db_session, enabled=False)
    assessment = await _assessment(db_session, patient, professional)
    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})
    assert resp.status_code == 403
    llm.assert_not_awaited()


async def test_unknown_assessment_returns_404(api_client, auth_headers, db_session, llm):
    await _flag(db_session)
    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(uuid4())})
    assert resp.status_code == 404


async def test_assessment_of_another_professional_returns_404(api_client, auth_headers, db_session, llm):
    await _flag(db_session)
    stranger = Professional(
        email="outra-fono-2@example.com", password_hash=hash_password("testpass123"),
        name="Dra. Outra", specialty_key="fono", specialty="Fonoaudiologia",
        council="CRFa", phone="11999990002", email_verified_at=datetime.now(UTC),
    )
    db_session.add(stranger)
    await db_session.flush()
    foreign_patient = Patient(
        professional_id=stranger.id, name="Paciente Confidencial", birth_date=date(2021, 1, 1),
        diagnosis_keys=[], status="ativo", start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign_patient)
    await db_session.commit()
    assessment = await _assessment(db_session, foreign_patient, stranger)

    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})

    assert resp.status_code == 404
    llm.assert_not_awaited()


async def test_suggests_goals_from_focused_assessment(
    api_client, auth_headers, patient, professional, db_session, llm
):
    await _flag(db_session)
    db_session.add(Goal(
        patient_id=patient.id, professional_id=professional.id, title="Pedir ajuda",
        area="Linguagem", progress=0, start_date=date(2026, 9, 1),
    ))
    await db_session.commit()
    assessment = await _assessment(db_session, patient, professional)

    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jobId"]
    assert body["goals"][0] == {
        "title": "Produzir /r/ em início de palavra",
        "area": "Fonologia",
        "rationale": "PCC 61% na imitação",
    }
    prompt = llm.await_args.args[0]
    assert "### Avaliação em foco" in prompt
    assert "- fonologia-imitacao: PCC — 61%" in prompt
    assert "Pedir ajuda (Linguagem)" in prompt
    assert llm.await_args.kwargs["output"] == "json"


async def test_invalid_then_valid_json_retries_once(
    api_client, auth_headers, patient, professional, db_session, llm
):
    await _flag(db_session)
    llm.side_effect = ["Não consegui gerar.", VALID]
    assessment = await _assessment(db_session, patient, professional)

    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})

    assert resp.status_code == 200
    assert llm.await_count == 2
    assert "Sua resposta anterior foi rejeitada" in llm.await_args_list[1].args[0]
    assert llm.await_args_list[0].kwargs["deadline"] == llm.await_args_list[1].kwargs["deadline"]


async def test_json_retry_shares_deadline_and_cancels_provider_on_timeout(
    api_client, auth_headers, patient, professional, db_session, monkeypatch
):
    await _flag(db_session)
    assessment = await _assessment(db_session, patient, professional)
    monkeypatch.setattr("app.services.ai_workflow_service.LLM_TIMEOUT_SECONDS", 0.1)
    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_api_key", "test-key")
    client = AsyncMock()
    cancelled = asyncio.Event()

    async def respond(**_kwargs):
        if client.chat.completions.create.await_count == 1:
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="sem json"))])
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.chat.completions.create.side_effect = respond
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: client)
    deadlines = []
    timeout_at = asyncio.timeout_at

    def record_timeout(deadline):
        deadlines.append(deadline)
        return timeout_at(deadline)

    monkeypatch.setattr("app.services.ai_service.asyncio.timeout_at", record_timeout)
    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})

    assert resp.status_code == 503, resp.text
    assert resp.headers["Retry-After"] == "60"
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert cancelled.is_set()
    assert client.close.await_count == 2


async def test_invalid_json_twice_returns_502_and_marks_job_failed(
    api_client, auth_headers, patient, professional, db_session, llm
):
    await _flag(db_session)
    llm.side_effect = ["sem json", '{"goals": []}']
    assessment = await _assessment(db_session, patient, professional)

    resp = await api_client.post(URL, headers=auth_headers, json={"assessmentId": str(assessment.id)})

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Não foi possível interpretar as sugestões da IA. Tente novamente."
    job = (await db_session.execute(select(AIJob).where(AIJob.job_type == "assessment-goals"))).scalar_one()
    assert job.status == "failed"
    assert json.loads(job.input_data) == {"assessmentId": str(assessment.id)}


def test_suggested_goals_are_trimmed_capped_and_cleaned():
    parsed = SuggestedGoals.model_validate({"goals": [
        {"title": "  " + "x" * 300, "area": "", "rationale": "r" * 700},
        {"title": "   ", "area": "Fala", "rationale": ""},
        *[{"title": f"Meta {i}", "area": "Fala", "rationale": ""} for i in range(10)],
    ]})
    first = parsed.goals[0]
    assert len(first.title) == 255
    assert first.area == "Geral"
    assert len(first.rationale) == 600
    assert all(goal.title for goal in parsed.goals)
    assert len(parsed.goals) == 8
