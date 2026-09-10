"""AssistantService — unified AI assistant (clínico + gestão) via tool-calling.

Orchestrates read-only queries against the logged-in professional's own data
using OpenCode Zen function calling. Adapted from myclinic-back's
management_assistant, simplified to the Korus Fono single-professional domain.

Pipeline: direct tool call → (if no tool) retry reinforced → (if still none)
fallback. Sanitizes the final reply against leaked tool markup.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, List, Optional

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.ai import ChatMessage, Conversation
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.assistant import ChatMetadata, ChatResponse
from app.services.assistant.format_reply import (
    is_leaked_tool_markup,
    sanitize_llm_plain_text,
)
from app.services.assistant.llm_client import create_opencode_client
from app.services.assistant.prompts import (
    DOMAIN_GLOSSARY,
    FALLBACK_REPLY,
    FALLBACK_SUGGESTIONS,
    FEW_SHOT_EXAMPLES,
    SYSTEM_PROMPT,
    build_retry_messages,
)
from app.services.assistant.query_heuristics import classify_query
from app.services.assistant.tools import (
    MAX_TOOL_CALLS,
    TOOL_DEFINITIONS,
    ToolExecutor,
    parse_tool_arguments,
)

logger = logging.getLogger(__name__)


class AssistantService:
    def __init__(
        self,
        db: AsyncSession,
        professional: Professional,
        conversation: Conversation,
        llm_client: Optional[AsyncOpenAI] = None,
    ):
        self.db = db
        self.professional = professional
        self.conversation = conversation
        self._client = llm_client

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = create_opencode_client(session_id=self.conversation.id)
        return self._client

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #

    async def _resolve_patient_context_line(self) -> str:
        """If the conversation has a linked patient, inject id+name for clinical tools."""
        if not self.conversation.patient_id:
            return ""
        patient = await self.db.get(Patient, self.conversation.patient_id)
        if patient is None or patient.professional_id != self.professional.id:
            return ""
        return (
            f"Paciente vinculado a esta conversa: {patient.name} (patient_id: {patient.id}). "
            "Use este id nas ferramentas clínicas a menos que o usuário pergunte sobre outro paciente."
        )

    def _build_llm_messages(self, history: List[ChatMessage], context_line: str) -> List[dict[str, Any]]:
        context = (
            f"Data de hoje: {date.today().isoformat()}. "
            "Use as ferramentas para obter dados reais antes de responder. "
            f"Especialidade do profissional: {self.professional.specialty or self.professional.specialty_key}."
        )
        if context_line:
            context += " " + context_line

        llm_messages: List[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": DOMAIN_GLOSSARY},
            {"role": "system", "content": FEW_SHOT_EXAMPLES},
            {"role": "system", "content": context},
        ]
        for msg in history:
            llm_messages.append({"role": msg.role, "content": msg.content})
        return llm_messages

    @staticmethod
    def _last_user_query(history: List[ChatMessage]) -> str:
        for msg in reversed(history):
            if msg.role == "user":
                return msg.content.strip()
        return history[-1].content.strip() if history else ""

    # ------------------------------------------------------------------ #
    # LLM calls
    # ------------------------------------------------------------------ #

    async def _call_tool_selection(
        self, messages: List[dict[str, Any]], *, temperature: float = 0.2
    ) -> Any:
        settings = get_settings()
        return await self.client.chat.completions.create(
            model=settings.opencode_model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=temperature,
        )

    async def _call_final(
        self, messages: List[dict[str, Any]], *, temperature: float = 0.3
    ) -> str:
        settings = get_settings()
        completion = await self.client.chat.completions.create(
            model=settings.opencode_model,
            messages=messages,
            temperature=temperature,
            tool_choice="none",
        )
        return completion.choices[0].message.content or ""

    # ------------------------------------------------------------------ #
    # Tool execution + formatting
    # ------------------------------------------------------------------ #

    def _tool_specs_from_llm_message(self, assistant_message: Any) -> List[dict[str, Any]]:
        specs: List[dict[str, Any]] = []
        for tool_call in assistant_message.tool_calls or []:
            specs.append(
                {
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "arguments": parse_tool_arguments(tool_call.function.arguments),
                }
            )
        return specs

    async def _execute_tools_and_format(
        self,
        llm_messages: List[dict[str, Any]],
        tool_specs: List[dict[str, Any]],
        executor: ToolExecutor,
        *,
        queried_at: datetime,
        pipeline: str,
        heuristic: str,
    ) -> ChatResponse:
        follow_up: List[dict[str, Any]] = list(llm_messages)
        assistant_tool_calls: List[dict[str, Any]] = []

        for index, spec in enumerate(tool_specs[:MAX_TOOL_CALLS]):
            call_id = spec.get("id") or f"call_{index}_{uuid.uuid4().hex[:8]}"
            fn_name = spec["name"]
            fn_args = spec.get("arguments") or {}
            assistant_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": json.dumps(fn_args, ensure_ascii=False),
                    },
                }
            )

        follow_up.append(
            {"role": "assistant", "content": None, "tool_calls": assistant_tool_calls}
        )

        for index, spec in enumerate(tool_specs[:MAX_TOOL_CALLS]):
            call_id = assistant_tool_calls[index]["id"]
            fn_name = spec["name"]
            fn_args = spec.get("arguments") or {}
            try:
                result = await executor.execute(fn_name, fn_args)
                content = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                logger.exception("Tool execution failed: %s", fn_name)
                content = json.dumps(
                    {"error": "Não foi possível obter esses dados no momento."},
                    ensure_ascii=False,
                )
            follow_up.append(
                {"role": "tool", "tool_call_id": call_id, "content": content}
            )

        raw_reply = await self._call_final(follow_up)
        if is_leaked_tool_markup(raw_reply):
            logger.warning(
                "Assistant model leaked tool markup in final response prof=%s tools=%s",
                self.professional.id,
                executor.tools_used,
            )
            raw_reply = FALLBACK_REPLY
        reply = sanitize_llm_plain_text(raw_reply) or sanitize_llm_plain_text(FALLBACK_REPLY)

        logger.info(
            "Assistant query prof=%s pipeline=%s heuristic=%s tools=%s",
            self.professional.id,
            pipeline,
            heuristic,
            executor.tools_used,
        )

        return ChatResponse(
            reply=reply,
            metadata=ChatMetadata(
                tools_used=executor.tools_used,
                date_from=executor.date_from,
                date_to=executor.date_to,
                queried_at=queried_at,
            ),
        )

    def _fallback_response(self, *, queried_at: datetime, executor: ToolExecutor) -> ChatResponse:
        return ChatResponse(
            reply=sanitize_llm_plain_text(FALLBACK_REPLY),
            metadata=ChatMetadata(
                tools_used=[],
                date_from=executor.date_from,
                date_to=executor.date_to,
                queried_at=queried_at,
            ),
            suggestions=FALLBACK_SUGGESTIONS,
        )

    # ------------------------------------------------------------------ #
    # Public entry
    # ------------------------------------------------------------------ #

    async def chat(self, user_message: str) -> ChatResponse:
        queried_at = datetime.now(timezone.utc)
        # Load recent history (limit to last ~20 messages to bound context).
        history = list(self.conversation.messages or [])

        # The handler already persists the user message before calling chat(),
        # so the last entry is the user's — but we operate on the conversation
        # state provided, which includes it.
        user_query = self._last_user_query(history) if history else user_message
        heuristic = classify_query(user_query)

        executor = ToolExecutor(self.db, self.professional)
        context_line = await self._resolve_patient_context_line()
        llm_messages = self._build_llm_messages(history, context_line)

        # 1) Direct tool selection.
        completion = await self._call_tool_selection(llm_messages)
        assistant_message = completion.choices[0].message
        tool_calls = assistant_message.tool_calls or []

        if tool_calls:
            tool_specs = self._tool_specs_from_llm_message(assistant_message)
            return await self._execute_tools_and_format(
                llm_messages,
                tool_specs,
                executor,
                queried_at=queried_at,
                pipeline="direct",
                heuristic=heuristic,
            )

        # 2) Retry with reinforced instruction (only when no tool was called).
        leaked = assistant_message.content or ""
        if is_leaked_tool_markup(leaked):
            logger.warning(
                "Assistant model leaked markup in first response prof=%s", self.professional.id
            )

        retry_messages = build_retry_messages(llm_messages, user_query)
        retry_completion = await self._call_tool_selection(retry_messages, temperature=0.1)
        retry_message = retry_completion.choices[0].message
        retry_tool_calls = retry_message.tool_calls or []

        if retry_tool_calls:
            tool_specs = self._tool_specs_from_llm_message(retry_message)
            return await self._execute_tools_and_format(
                llm_messages,
                tool_specs,
                executor,
                queried_at=queried_at,
                pipeline="retry",
                heuristic=heuristic,
            )

        # 3) Fallback.
        logger.info(
            "Assistant fallback prof=%s heuristic=%s", self.professional.id, heuristic
        )
        return self._fallback_response(queried_at=queried_at, executor=executor)