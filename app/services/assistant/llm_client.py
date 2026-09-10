"""OpenCode Zen LLM client (OpenAI-compatible chat completions with tool calling)."""

from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from openai import AsyncOpenAI

from app.core.config import get_settings

# Prefixes routed to non-chat/completions endpoints (no OpenAI tool_calls).
OPENCODE_INCOMPATIBLE_MODEL_PREFIXES = ("claude-", "gpt-", "gemini-")


def opencode_request_headers(session_id: UUID | None = None) -> dict[str, str]:
    """Identify Korus and keep routing stable for each conversation or generation."""
    return {
        "User-Agent": "korus-fono/0.1.0",
        "x-opencode-session": str(session_id or uuid4()),
    }


def create_opencode_client(
    api_key: Optional[str] = None, *, session_id: UUID | None = None
) -> AsyncOpenAI:
    """Create an AsyncOpenAI client pointed at OpenCode Zen.

    Validates both the API key and the configured model so callers get a
    single, consistent 503 instead of failing later mid-request.
    """
    settings = get_settings()
    key = api_key or settings.opencode_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assistente de IA não configurado (OPENCODE_API_KEY ausente).",
        )
    validate_opencode_model(settings.opencode_model)
    base_url = settings.opencode_base_url.rstrip("/")
    return AsyncOpenAI(
        api_key=key,
        base_url=base_url,
        timeout=settings.assistant_llm_timeout_seconds,
        default_headers=opencode_request_headers(session_id),
    )


def validate_opencode_model(model: str) -> None:
    """Reject models that Zen routes outside /v1/chat/completions (no tool_calls)."""
    lowered = model.lower()
    for prefix in OPENCODE_INCOMPATIBLE_MODEL_PREFIXES:
        if lowered.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Modelo '{model}' usa endpoint incompatível com function calling. "
                    "Escolha um modelo OpenCode Zen com /v1/chat/completions "
                    "(ex.: deepseek-v4-flash, kimi-k2.5, glm-5). "
                    "Veja https://opencode.ai/docs/zen/"
                ),
            )
