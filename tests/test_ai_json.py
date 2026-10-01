from datetime import date

import pytest
from pydantic import BaseModel

from app.models.assessment import Assessment
from app.models.battery import BatterySubformAssessment
from app.services.ai_context import build_focused_assessment_section
from app.services.ai_json import LLMJsonError, extract_json_object, parse_llm_json


class _Payload(BaseModel):
    goals: list[str]


def test_extracts_object_from_code_fence():
    assert extract_json_object('```json\n{"goals": ["a"]}\n```') == {"goals": ["a"]}


def test_extracts_object_after_preamble():
    assert extract_json_object('Claro! Aqui está: {"goals": ["a"]} Espero ajudar.') == {"goals": ["a"]}


@pytest.mark.parametrize("content", ["sem json", '{"goals": [', '["a"]'])
def test_invalid_content_raises(content):
    with pytest.raises(LLMJsonError):
        extract_json_object(content)


def test_validation_error_becomes_llm_json_error():
    with pytest.raises(LLMJsonError):
        parse_llm_json('{"goals": "a"}', _Payload)


def test_focused_assessment_lists_only_completed_subtests():
    assessment = Assessment(
        protocol_id="abfw", date=date(2026, 9, 20), result="Alteração fonológica",
        percentage=58, interpretation="Processos de simplificação",
        scores={"domains": {"fono": {"title": "Fonologia", "percentage": 58}}},
    )
    assessment.protocol = None
    assessment.battery_subforms = [
        BatterySubformAssessment(
            instrument_slug="abfw", subform_slug="fonologia-imitacao", status="completed",
            scores={"domains": {"pcc": {"title": "PCC", "percentage": 61}}},
        ),
        BatterySubformAssessment(
            instrument_slug="abfw", subform_slug="vocabulario", status="pending", scores=None,
        ),
    ]

    text = build_focused_assessment_section(assessment)

    assert text.startswith("### Avaliação em foco")
    assert "Protocolo: abfw" in text
    assert "Resultado: Alteração fonológica (58%)" in text
    assert "Interpretação: Processos de simplificação" in text
    assert "Domínios: Fonologia — 58%" in text
    assert "- fonologia-imitacao: PCC — 61%" in text
    assert "vocabulario" not in text
