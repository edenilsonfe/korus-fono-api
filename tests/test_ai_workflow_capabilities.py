from pathlib import Path

from app.constants.ai_flags import AI_ASSESSMENT_GOALS_FLAG, AI_EVOLUTION_DICTATION_FLAG
from app.core.config import get_settings
from app.models.feature_flag import FeatureFlag


def _settings(monkeypatch, *, llm: bool, audio: bool = False):
    monkeypatch.setattr(
        "app.api.v1.ai.get_settings",
        lambda: get_settings().model_copy(
            update={
                "opencode_api_key": "fake" if llm else "",
                "audio_transcription_api_key": "fake" if audio else "",
            }
        ),
    )


async def test_capabilities_report_workflow_flags(api_client, auth_headers, db_session, monkeypatch):
    _settings(monkeypatch, llm=True)
    db_session.add(FeatureFlag(key=AI_EVOLUTION_DICTATION_FLAG, description="t", enabled_global=True))
    db_session.add(FeatureFlag(key=AI_ASSESSMENT_GOALS_FLAG, description="t", enabled_global=False))
    await db_session.commit()

    resp = await api_client.get("/api/v1/ai/capabilities", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "llmEnabled": True,
        "audioTranscriptionEnabled": False,
        "evolutionDictationEnabled": True,
        "assessmentGoalsEnabled": False,
    }


async def test_capabilities_hide_workflows_without_llm(api_client, auth_headers, db_session, monkeypatch):
    _settings(monkeypatch, llm=False, audio=True)
    db_session.add(FeatureFlag(key=AI_EVOLUTION_DICTATION_FLAG, description="t", enabled_global=True))
    db_session.add(FeatureFlag(key=AI_ASSESSMENT_GOALS_FLAG, description="t", enabled_global=True))
    await db_session.commit()

    body = (await api_client.get("/api/v1/ai/capabilities", headers=auth_headers)).json()

    assert body["evolutionDictationEnabled"] is False
    assert body["assessmentGoalsEnabled"] is False
    assert body["audioTranscriptionEnabled"] is True


async def test_capabilities_without_flag_rows(api_client, auth_headers, monkeypatch):
    _settings(monkeypatch, llm=True)
    body = (await api_client.get("/api/v1/ai/capabilities", headers=auth_headers)).json()
    assert body["evolutionDictationEnabled"] is False
    assert body["assessmentGoalsEnabled"] is False


def test_migration_registers_disabled_flags_after_current_head():
    source = Path("alembic/versions/ai20261001a_ai_workflow_flags.py").read_text(encoding="utf-8")
    assert 'revision = "ai20261001a"' in source
    assert 'down_revision = "gc20260930a"' in source
    assert '"ai_evolution_dictation"' in source
    assert '"ai_assessment_goals"' in source
    assert "false) ON CONFLICT (key) DO NOTHING" in source
    assert "DELETE FROM feature_flags WHERE key = :key" in source
