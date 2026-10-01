"""Extração e validação de respostas JSON de LLM."""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMJsonError(ValueError):
    """A resposta do LLM não contém um JSON válido para o modelo esperado."""


def extract_json_object(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start = text.find("{")
    if start == -1:
        raise LLMJsonError("Nenhum objeto JSON encontrado.")
    try:
        value, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise LLMJsonError(f"JSON inválido: {exc.msg}.") from exc
    if not isinstance(value, dict):
        raise LLMJsonError("O JSON precisa ser um objeto.")
    return value


def parse_llm_json(content: str, model: type[T]) -> T:
    data = extract_json_object(content)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise LLMJsonError(f"Formato inesperado: {exc.errors()[0]['msg']}.") from exc
