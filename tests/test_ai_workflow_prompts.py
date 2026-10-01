from app.schemas.ai import AIToolRequest
from app.services.ai_prompts import AI_TOOL_SPECS, build_request_prompt, build_tool_prompt


def test_evolution_draft_spec_shape():
    spec = AI_TOOL_SPECS["evolution-draft"]
    assert spec.sections == ["identity", "goals", "evolutions"]
    assert spec.limits == {"evolutions": 2}
    assert spec.output == "plain"
    for title in (
        "Objetivos trabalhados:",
        "Atividades e estratégias:",
        "Desempenho e respostas:",
        "Orientações à família:",
        "Próximos passos:",
    ):
        assert title in spec.prompt_template
    assert "Não invente" in spec.system


def test_prompt_with_input_text_also_appends_context():
    spec = AI_TOOL_SPECS["evolution-draft"]
    prompt = build_tool_prompt(
        spec,
        context="### Metas terapêuticas\n- Pedir ajuda (Linguagem)",
        input_text="Trabalhamos /r/ com 7 acertos em 10.",
    )
    assert "Trabalhamos /r/ com 7 acertos em 10." in prompt
    assert "Pedir ajuda (Linguagem)" in prompt
    assert prompt.index("Trabalhamos /r/") < prompt.index("Contexto clínico:")


def test_session_summary_still_has_no_context_block():
    _spec, prompt = build_request_prompt(
        "session-summary", AIToolRequest(session_notes="Paciente colaborativo.")
    )
    assert "Contexto clínico:" not in prompt
