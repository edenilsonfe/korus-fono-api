import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.constants.ai_flags import AI_EVOLUTION_DICTATION_FLAG
from app.core.config import get_settings
from app.core.security import hash_password
from app.models.ai import AIJob
from app.models.evolution import Evolution
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session as ClinicalSession

URL = "/api/v1/ai/evolution-draft"
NOTES = "Trabalhamos /r/ em início de palavra com 7 acertos em 10."


async def _flag(db_session, enabled=True):
    db_session.add(FeatureFlag(key=AI_EVOLUTION_DICTATION_FLAG, description="t", enabled_global=enabled))
    await db_session.commit()


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value="Objetivos trabalhados:\n- Produção de /r/")
    monkeypatch.setattr("app.services.ai_workflow_service.run_llm", mock)
    return mock


async def _patient_for(db_session, professional, name="Outra Criança"):
    other = Patient(
        professional_id=professional.id,
        name=name,
        birth_date=date(2021, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(other)
    await db_session.commit()
    return other


async def test_flag_disabled_returns_403_without_calling_llm(api_client, auth_headers, patient, db_session, llm):
    await _flag(db_session, enabled=False)
    resp = await api_client.post(URL, headers=auth_headers, json={"patientId": str(patient.id), "notes": NOTES})
    assert resp.status_code == 403
    llm.assert_not_awaited()


async def test_draft_uses_goals_and_two_latest_evolutions(
    api_client, auth_headers, patient, professional, db_session, llm
):
    await _flag(db_session)
    db_session.add(Goal(
        patient_id=patient.id, professional_id=professional.id,
        title="Produzir /r/ em início de palavra", area="Fonologia", progress=20,
        start_date=date(2026, 9, 1),
    ))
    for day in (1, 2, 3):
        db_session.add(Evolution(
            patient_id=patient.id, professional_id=professional.id,
            date=datetime(2026, 9, day, tzinfo=UTC), title=f"Evo {day}", content=f"Conteúdo {day}",
        ))
    await db_session.commit()

    resp = await api_client.post(URL, headers=auth_headers, json={"patientId": str(patient.id), "notes": NOTES})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["result"].startswith("Objetivos trabalhados:")
    prompt, system = llm.await_args.args[0], llm.await_args.args[1]
    assert NOTES in prompt
    assert "Produzir /r/ em início de palavra" in prompt
    assert "Conteúdo 3" in prompt and "Conteúdo 2" in prompt
    assert "Conteúdo 1" not in prompt
    assert "Não invente" in system
    assert llm.await_args.kwargs["output"] == "plain"


async def test_job_keeps_hash_not_notes(api_client, auth_headers, patient, db_session, llm):
    await _flag(db_session)
    await api_client.post(URL, headers=auth_headers, json={"patientId": str(patient.id), "notes": f"  {NOTES}  "})
    job = (await db_session.execute(select(AIJob).where(AIJob.job_type == "evolution-draft"))).scalar_one()
    assert NOTES not in job.input_data
    assert json.loads(job.input_data)["notesSha256"] == hashlib.sha256(NOTES.encode("utf-8")).hexdigest()
    assert job.status == "completed"


async def test_session_of_another_patient_returns_404(
    api_client, auth_headers, patient, professional, db_session, llm
):
    await _flag(db_session)
    other = await _patient_for(db_session, professional)
    foreign_session = ClinicalSession(
        patient_id=other.id, professional_id=professional.id,
        date=datetime(2026, 9, 30, 13, tzinfo=UTC), type="Terapia",
    )
    db_session.add(foreign_session)
    await db_session.commit()

    resp = await api_client.post(URL, headers=auth_headers, json={
        "patientId": str(patient.id), "sessionId": str(foreign_session.id), "notes": NOTES,
    })

    assert resp.status_code == 404
    llm.assert_not_awaited()


async def test_patient_of_another_professional_returns_404(api_client, auth_headers, db_session, llm):
    await _flag(db_session)
    stranger = Professional(
        email="outra-fono@example.com", password_hash=hash_password("testpass123"),
        name="Dra. Outra", specialty_key="fono", specialty="Fonoaudiologia",
        council="CRFa", phone="11999990001", email_verified_at=datetime.now(UTC),
    )
    db_session.add(stranger)
    await db_session.commit()
    foreign = await _patient_for(db_session, stranger, name="Paciente Confidencial")

    resp = await api_client.post(URL, headers=auth_headers, json={"patientId": str(foreign.id), "notes": NOTES})

    assert resp.status_code == 404
    assert "Paciente Confidencial" not in resp.text
    llm.assert_not_awaited()


async def test_blank_notes_are_rejected(api_client, auth_headers, patient, db_session, llm):
    await _flag(db_session)
    resp = await api_client.post(URL, headers=auth_headers, json={"patientId": str(patient.id), "notes": "   "})
    assert resp.status_code == 422


async def test_llm_not_configured_returns_503(api_client, auth_headers, patient, db_session, monkeypatch):
    await _flag(db_session)
    monkeypatch.setattr(
        "app.services.ai_service.get_settings",
        lambda: get_settings().model_copy(update={"opencode_api_key": ""}),
    )
    resp = await api_client.post(URL, headers=auth_headers, json={"patientId": str(patient.id), "notes": NOTES})
    assert resp.status_code == 503


async def test_transcribe_records_session_id(api_client, auth_headers, patient, db_session, monkeypatch):
    transcription = SimpleNamespace(
        text="texto transcrito", filename="ditado.webm", content_type="audio/webm",
        size_bytes=3, sha256="abc",
    )
    monkeypatch.setattr("app.api.v1.ai.transcribe_audio", AsyncMock(return_value=transcription))
    session_id = uuid4()

    resp = await api_client.post(
        "/api/v1/ai/transcribe", headers=auth_headers,
        data={"patientId": str(patient.id), "sessionId": str(session_id)},
        files={"file": ("ditado.webm", b"abc", "audio/webm")},
    )

    assert resp.status_code == 200, resp.text
    job = (await db_session.execute(select(AIJob).where(AIJob.job_type == "transcribe"))).scalar_one()
    assert json.loads(job.input_data)["sessionId"] == str(session_id)


async def test_transcribe_rejects_invalid_session_id(api_client, auth_headers, patient, monkeypatch):
    monkeypatch.setattr("app.api.v1.ai.transcribe_audio", AsyncMock())
    resp = await api_client.post(
        "/api/v1/ai/transcribe", headers=auth_headers,
        data={"patientId": str(patient.id), "sessionId": "nao-e-uuid"},
        files={"file": ("ditado.webm", b"abc", "audio/webm")},
    )
    assert resp.status_code == 422
