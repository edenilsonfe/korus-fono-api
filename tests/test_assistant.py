"""Tests for the unified AI assistant orchestration (LLM mocked).

Standalone setup: creates only the tables needed on an in-memory SQLite DB.
The LLM client is mocked so we never call OpenCode (and never get billed).
Validates: direct tool-call path, no-tool → retry → tool, no-tool → retry →
no-tool → fallback, leaked-markup sanitization, patient ownership guard,
search_patient_by_name scoping, and rate limiting (429).
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.models.ai import AIReport, ChatMessage, Conversation
from app.models.appointment import Appointment
from app.models.assessment import Assessment, ProtocolCatalog
from app.models.evolution import Evolution
from app.models.goal import Goal, ClinicalDomainSnapshot
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
# conftest supplies SQLite adapters for the PostgreSQL JSON/ARRAY columns.
from app.schemas.assistant import ChatResponse
from app.services.assistant.assistant_service import AssistantService
from app.services.assistant import rate_limit as rate_limit_module
from app.services.assistant import prompts as assistant_prompts
from app.services.assistant.conversation_patient import bind_conversation_patient
from app.services.assistant.rate_limit import enforce_assistant_rate_limit
from app.services.assistant.tools import ToolExecutor

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    AIReport.__table__,
                    Assessment.__table__,
                    ProtocolCatalog.__table__,
                    Patient.__table__,
                    Appointment.__table__,
                    Session.__table__,
                    Evolution.__table__,
                    Goal.__table__,
                    ClinicalDomainSnapshot.__table__,
                    Conversation.__table__,
                    ChatMessage.__table__,
                ],
            )
        )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _make_professional(db, *, email="prof@x.com", specialty="fono"):
    pro = Professional(
        email=email,
        password_hash="x",
        name="Dra. Teste",
        specialty_key=specialty,
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990000",
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


async def _make_patient(db, professional, *, name="João", status="ativo"):
    p = Patient(
        professional_id=professional.id,
        name=name,
        birth_date=date.today().replace(year=date.today().year - 4),
        diagnosis_keys=["tea"],
        status=status,
        start_date=date.today() - timedelta(days=90),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _make_conversation(db, professional, patient=None):
    conv = Conversation(
        professional_id=professional.id,
        patient_id=patient.id if patient else None,
        title="Conv",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv, attribute_names=["messages"])
    return conv


def _history(conv, content):
    """Append a user message to the conversation in-memory (mirrors handler)."""
    msg = ChatMessage(conversation_id=conv.id, role="user", content=content)
    conv.messages = list(conv.messages or []) + [msg]
    return conv


def _mock_tool_call_msg(name, args, call_id="call_1"):
    """Build an assistant 'message' with tool_calls, like the OpenAI SDK returns."""
    fn = MagicMock()
    fn.name = name
    import json

    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.id = call_id
    tc.function = fn
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = None
    choice = MagicMock()
    choice.message = msg
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _mock_text_msg(text):
    """Build an assistant 'message' with content and no tool_calls."""
    msg = MagicMock()
    msg.tool_calls = None
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    completion = MagicMock()
    completion.choices = [choice]
    return completion


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_direct_tool_call_executes_and_returns(db):
    pro = await _make_professional(db)
    p = await _make_patient(db, pro)
    # Seed a session so get_appointment_kpis(completion) returns something.
    db.add(Session(
        patient_id=p.id, professional_id=pro.id,
        date=datetime.now(timezone.utc), duration=45, type="atendimento",
        objectives=[], notes="",
    ))
    await db.commit()

    conv = await _make_conversation(db, pro)
    _history(conv, "Quantas sessões realizei este mês?")

    service = AssistantService(db, pro, conv, llm_client=AsyncMock())
    # 1st call: model calls a tool. 2nd call: final text reply.
    service._call_tool_selection = AsyncMock(
        return_value=_mock_tool_call_msg("get_appointment_kpis", {"metric": "completion"})
    )
    service._call_final = AsyncMock(return_value="Você realizou 1 sessão neste mês.")

    resp = await service.chat("Quantas sessões realizei este mês?")
    assert resp.reply == "Você realizou 1 sessão neste mês."
    assert "get_appointment_kpis" in resp.metadata.tools_used
    assert resp.suggestions is None


async def test_no_tool_then_retry_calls_tool(db):
    pro = await _make_professional(db)
    p = await _make_patient(db, pro)
    conv = await _make_conversation(db, pro)
    _history(conv, "como está minha clínica")

    service = AssistantService(db, pro, conv, llm_client=AsyncMock())
    calls = {"tool_select": 0, "final": 0}
    direct = _mock_text_msg("panorama segue normal")  # 1st: no tool_calls
    retry = _mock_tool_call_msg("get_dashboard_stats", {})  # retry: calls tool
    final = "Sua clínica tem 1 paciente ativo."

    async def _ts(messages, temperature=0.2):
        calls["tool_select"] += 1
        return direct if calls["tool_select"] == 1 else retry

    async def _tf(messages, temperature=0.3):
        calls["final"] += 1
        return final

    service._call_tool_selection = _ts
    service._call_final = _tf

    resp = await service.chat("como está minha clínica")
    assert resp.reply == final
    assert "get_dashboard_stats" in resp.metadata.tools_used
    assert calls["tool_select"] == 2  # direct + retry


async def test_no_tool_no_retry_falls_back(db):
    pro = await _make_professional(db)
    conv = await _make_conversation(db, pro)
    _history(conv, "qual o sentido da vida")

    service = AssistantService(db, pro, conv, llm_client=AsyncMock())
    service._call_tool_selection = AsyncMock(return_value=_mock_text_msg("não sei"))
    service._call_final = AsyncMock(return_value="não deveria chegar aqui")

    resp = await service.chat("qual o sentido da vida")
    assert "Não consigo responder" in resp.reply
    assert resp.metadata.tools_used == []
    assert resp.suggestions is not None and len(resp.suggestions) > 0


async def test_leaked_markup_in_final_falls_back(db):
    pro = await _make_professional(db)
    p = await _make_patient(db, pro)
    conv = await _make_conversation(db, pro)
    _history(conv, "sessões")

    service = AssistantService(db, pro, conv, llm_client=AsyncMock())
    service._call_tool_selection = AsyncMock(
        return_value=_mock_tool_call_msg("get_appointment_kpis", {"metric": "all"})
    )
    # Final reply leaks tool markup → must be replaced by fallback.
    service._call_final = AsyncMock(return_value="<|tool_call|>get_dashboard_stats()")

    resp = await service.chat("sessões")
    assert "<|tool_call" not in resp.reply
    assert "Não consigo responder" in resp.reply  # sanitize of markup → '' → fallback


async def test_tool_leaked_in_first_response_handled(db):
    pro = await _make_professional(db)
    conv = await _make_conversation(db, pro)
    _history(conv, "teste")

    service = AssistantService(db, pro, conv, llm_client=AsyncMock())
    # 1st response leaks markup (no tool_calls). Retry: no tool. → fallback.
    service._call_tool_selection = AsyncMock(
        side_effect=[_mock_text_msg("<|tool_call|>foo"), _mock_text_msg("ainda sem tool")]
    )
    service._call_final = AsyncMock()

    resp = await service.chat("teste")
    assert resp.metadata.tools_used == []
    assert "Não consigo responder" in resp.reply


async def test_patient_ownership_guard_in_tool_executor(db):
    pro = await _make_professional(db)
    other_pro = await _make_professional(db, email="other@x.com")
    # Patient belongs to other_pro.
    other_patient = await _make_patient(db, other_pro, name="Pertence a outro")

    executor = ToolExecutor(db, pro)
    result = await executor.execute(
        "get_patient_context", {"patient_id": str(other_patient.id)}
    )
    assert result == {"error": "Paciente não encontrado na sua carteira."}
    # Tool still recorded as used (it ran), but no data leaked.
    assert "get_patient_context" in executor.tools_used


async def test_search_patient_scoped_to_professional(db):
    pro = await _make_professional(db)
    other_pro = await _make_professional(db, email="other2@x.com")
    await _make_patient(db, pro, name="Ana Silva")
    await _make_patient(db, other_pro, name="Ana Outra")

    executor = ToolExecutor(db, pro)
    result = await executor.execute("search_patient_by_name", {"name": "Ana"})
    names = [p["name"] for p in result["patients"]]
    assert "Ana Silva" in names
    assert "Ana Outra" not in names  # scoped — does not leak


async def test_get_patient_context_returns_owned(db):
    pro = await _make_professional(db)
    p = await _make_patient(db, pro, name="João")

    executor = ToolExecutor(db, pro)
    result = await executor.execute("get_patient_context", {"patient_id": str(p.id)})
    assert "error" not in result
    assert result["patient_name"] == "João"
    assert "context" in result


async def test_get_inactive_patients_excludes_recent(db):
    pro = await _make_professional(db)
    inactive = await _make_patient(db, pro, name="Parado")
    active = await _make_patient(db, pro, name="Recente")
    # recent session for 'active'
    db.add(Session(
        patient_id=active.id, professional_id=pro.id,
        date=datetime.now(timezone.utc), duration=45, type="atendimento",
        objectives=[], notes="",
    ))
    await db.commit()

    executor = ToolExecutor(db, pro)
    result = await executor.execute("get_inactive_patients", {"inactive_days": 30})
    names = [p["name"] for p in result["patients"]]
    assert "Parado" in names
    assert "Recente" not in names


def _force_memory_rate_limit_fallback(*_args, **_kwargs):
    raise ConnectionError("Redis unavailable in tests")


@pytest.fixture
def assistant_rate_limit_env(monkeypatch):
    """Force in-memory rate limiting with a low hourly cap."""
    rate_limit_module._in_memory_buckets.clear()
    monkeypatch.setattr(rate_limit_module, "_redis_check", _force_memory_rate_limit_fallback)
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_rate_limit_per_hour", 2)
    yield
    rate_limit_module._in_memory_buckets.clear()


def test_rate_limit_allows_requests_under_limit(assistant_rate_limit_env):
    pro_id = "prof-rate-limit-ok"
    enforce_assistant_rate_limit(pro_id)
    enforce_assistant_rate_limit(pro_id)


def test_rate_limit_raises_429_when_exceeded(assistant_rate_limit_env):
    pro_id = "prof-rate-limit-blocked"
    enforce_assistant_rate_limit(pro_id)
    enforce_assistant_rate_limit(pro_id)

    with pytest.raises(HTTPException) as exc_info:
        enforce_assistant_rate_limit(pro_id)

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers.get("Retry-After") == "3600"
    assert "Limite de mensagens" in exc_info.value.detail


def test_rate_limit_redis_counter(monkeypatch):
    """When Redis is available, enforce uses the shared counter result."""
    calls = {"n": 0}

    def fake_redis_check(_key, max_requests, _window_seconds):
        calls["n"] += 1
        return calls["n"] <= max_requests

    monkeypatch.setattr(rate_limit_module, "_redis_check", fake_redis_check)
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_rate_limit_per_hour", 2)

    pro_id = "prof-rate-limit-redis"
    enforce_assistant_rate_limit(pro_id)
    enforce_assistant_rate_limit(pro_id)

    with pytest.raises(HTTPException) as exc_info:
        enforce_assistant_rate_limit(pro_id)

    assert exc_info.value.status_code == 429
    assert calls["n"] == 3


async def test_bind_conversation_patient_sets_when_empty(db):
    pro = await _make_professional(db)
    p = await _make_patient(db, pro, name="Lucas Costa")
    conv = await _make_conversation(db, pro, patient=None)
    assert conv.patient_id is None
    await bind_conversation_patient(db, pro, conv, str(p.id))
    await db.commit()
    await db.refresh(conv)
    assert conv.patient_id == p.id


async def test_bind_conversation_patient_keeps_existing(db):
    pro = await _make_professional(db)
    p1 = await _make_patient(db, pro, name="Lucas")
    p2 = await _make_patient(db, pro, name="Ana")
    conv = await _make_conversation(db, pro, patient=p1)
    await bind_conversation_patient(db, pro, conv, str(p2.id))
    await db.commit()
    await db.refresh(conv)
    assert conv.patient_id == p1.id


async def test_bind_conversation_patient_rejects_foreign(db):
    pro = await _make_professional(db, email="a@x.com")
    other = await _make_professional(db, email="b@x.com")
    foreign = await _make_patient(db, other, name="Outro")
    conv = await _make_conversation(db, pro, patient=None)
    with pytest.raises(HTTPException) as exc:
        await bind_conversation_patient(db, pro, conv, str(foreign.id))
    assert exc.value.status_code in (403, 404)
    assert conv.patient_id is None


async def test_bind_conversation_patient_noop_when_none(db):
    pro = await _make_professional(db)
    conv = await _make_conversation(db, pro, patient=None)
    await bind_conversation_patient(db, pro, conv, None)
    assert conv.patient_id is None


def test_prompts_cover_name_followup_and_goals_read():
    blob = "\n".join(
        [
            assistant_prompts.SYSTEM_PROMPT,
            assistant_prompts.DOMAIN_GLOSSARY,
            assistant_prompts.FEW_SHOT_EXAMPLES,
        ]
    )
    assert "search_patient_by_name" in blob
    assert "estou falando do lucas costa" in blob.lower()
    assert "não invente metas novas" in blob.lower()
    assert "get_patient_goals" in blob
    assert "get_patient_evolutions" in blob
