"""Tests for AI tool specs, sanitization and endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
import httpx
from fastapi import HTTPException
from openai import AuthenticationError, RateLimitError

from app.core.config import get_settings
from app.services.ai_prompts import AI_TOOL_SPECS, build_request_prompt, build_tool_prompt
from app.services.assistant.format_reply import sanitize_llm_markdown
from app.services.assistant import rate_limit as rate_limit_module
from app.services.assistant.rate_limit import enforce_assistant_rate_limit
from app.schemas.ai import AIToolRequest


def test_tool_specs_cover_expected_keys():
    expected = {
        "report:clinico",
        "report:escolar",
        "report:pais",
        "report:evolutivo",
        "therapy-plan",
        "suggest-goals",
        "clinical-trends",
        "session-summary",
        "proofread",
    }
    assert set(AI_TOOL_SPECS) == expected


def test_report_clinico_sections_and_limits():
    spec = AI_TOOL_SPECS["report:clinico"]
    assert spec.sections == [
        "identity",
        "assessments",
        "evolutions",
        "goals",
        "anamnesis",
        "attendance",
    ]
    assert spec.limits["evolutions"] == 8
    assert spec.output == "markdown"


def test_session_summary_uses_body_text_only():
    spec, prompt = build_request_prompt(
        "session-summary",
        AIToolRequest(session_notes="Paciente colaborativo na sessão."),
    )
    assert spec.sections == []
    assert "Paciente colaborativo" in prompt
    assert "Contexto clínico" not in prompt


def test_markdown_sanitizer_preserves_headings():
    raw = "## Identificação\n\n- Item **importante**\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    cleaned = sanitize_llm_markdown(raw)
    assert "## Identificação" in cleaned
    assert "**importante**" in cleaned
    assert "| A | B |" in cleaned


def test_markdown_sanitizer_strips_code_fence_wrapper():
    raw = "```markdown\n## Título\n\nConteúdo\n```"
    cleaned = sanitize_llm_markdown(raw)
    assert cleaned.startswith("## Título")
    assert "```" not in cleaned


def test_markdown_sanitizer_clears_leaked_tool_markup():
    raw = "<|tool_call|>get_patient_context()"
    assert sanitize_llm_markdown(raw) == ""


@pytest.mark.asyncio
async def test_run_llm_output_modes():
    from app.services.ai_service import run_llm

    with patch("app.services.ai_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.opencode_api_key = "test-key"
        settings.opencode_base_url = "http://example.com"
        settings.opencode_model = "test-model"
        settings.assistant_llm_timeout_seconds = 30

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_response = AsyncMock()
            mock_response.choices = [AsyncMock(message=AsyncMock(content="## Título\n\n**Bold**"))]
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            markdown = await run_llm("prompt", system="sys", output="markdown")
            assert "## Título" in markdown

            mock_response.choices[0].message.content = "## Título\n\n**Bold**"
            plain = await run_llm("prompt", system="sys", output="plain")
            assert "##" not in plain
            assert "Bold" in plain


@pytest.mark.asyncio
async def test_run_llm_rejects_unconfigured_provider_instead_of_simulating():
    from app.services.ai_service import run_llm

    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_settings.return_value.opencode_api_key = ""

        with pytest.raises(HTTPException) as exc_info:
            await run_llm("dados clínicos reais")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Ferramentas de IA não configuradas."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error",
    [
        RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "https://provider")),
            body={"error": {"type": "FreeUsageLimitError"}},
        ),
        AuthenticationError(
            "insufficient credits",
            response=httpx.Response(401, request=httpx.Request("POST", "https://provider")),
            body={"error": {"type": "CreditsError"}},
        ),
    ],
)
async def test_run_llm_translates_provider_capacity_errors_to_safe_json_error(provider_error):
    from app.services.ai_service import run_llm

    with patch("app.services.ai_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.opencode_api_key = "test-key"
        settings.opencode_base_url = "https://provider"
        settings.opencode_model = "test-model"
        settings.assistant_llm_timeout_seconds = 30

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=provider_error)

            with pytest.raises(HTTPException) as exc_info:
                await run_llm("dados clínicos reais")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "Serviço de IA temporariamente indisponível. Tente novamente em alguns minutos."
    )
    assert exc_info.value.headers == {"Retry-After": "60"}


def _force_memory_rate_limit_fallback(*_args, **_kwargs):
    raise ConnectionError("Redis unavailable in tests")


@pytest.fixture
def assistant_rate_limit_env(monkeypatch):
    rate_limit_module._in_memory_buckets.clear()
    monkeypatch.setattr(rate_limit_module, "_redis_check", _force_memory_rate_limit_fallback)
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_rate_limit_per_hour", 1)
    yield
    rate_limit_module._in_memory_buckets.clear()


def test_rate_limit_raises_429_for_tools(assistant_rate_limit_env):
    pro_id = "prof-tools-rate-limit"
    enforce_assistant_rate_limit(pro_id)
    with pytest.raises(HTTPException) as exc_info:
        enforce_assistant_rate_limit(pro_id)
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers.get("Retry-After") == "3600"


def test_build_tool_prompt_appends_extra_instructions():
    spec = AI_TOOL_SPECS["clinical-trends"]
    prompt = build_tool_prompt(spec, context="ctx", extra_prompt="Foque no último trimestre.")
    assert "Contexto clínico" in prompt
    assert "Instruções adicionais: Foque no último trimestre." in prompt


def test_proofread_spec_is_plain_output():
    spec = AI_TOOL_SPECS["proofread"]
    assert spec.output == "plain"
    assert spec.sections == []

