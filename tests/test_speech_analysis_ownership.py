"""Ownership regression test for POST /ai/speech-analysis.

Ensures `_run_tool_job` checks patient ownership (`get_patient_for_professional`)
before building any patient context, so a cross-tenant `patientId` never loads
another professional's PHI into the prompt.

Standalone setup: creates only the tables needed on an in-memory SQLite DB,
mirroring the pattern in test_session_evolutions_idor.py. `EntitlementMiddleware`
opens its own session directly from `AsyncSessionLocal` (not the FastAPI `get_db`
override), so it is monkeypatched to the same in-memory engine too.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.ai import AIJob
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session as ClinicalSession


# Patient.diagnosis_keys / Assessment fields are Postgres JSONB columns and
# Session.objectives is a Postgres ARRAY column; SQLAlchemy 2.x can't render
# either for SQLite CREATE TABLE. Map both to plain JSON for this in-memory
# test DB only (build_patient_context reads Session/Goal/Evolution/Assessment).
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kw):
    return "JSON"


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


async def _engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Patient.__table__,
                    AIJob.__table__,
                    ClinicalSession.__table__,
                    Goal.__table__,
                    Evolution.__table__,
                    Assessment.__table__,
                ],
            )
        )
    return eng


async def _make_professional(db, *, email):
    pro = Professional(
        email=email,
        password_hash="x",
        name="Dra. Teste",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


async def _make_patient(db, professional, *, name):
    p = Patient(
        professional_id=professional.id,
        name=name,
        birth_date=date.today().replace(year=date.today().year - 4),
        diagnosis_keys=["tea"],
        status="ativo",
        start_date=date.today() - timedelta(days=90),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


def _auth_headers(professional):
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


async def _client(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    # EntitlementMiddleware bypasses the get_db override and opens its own
    # session from AsyncSessionLocal — point it at the same test engine.
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear_override():
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_speech_analysis_cross_tenant_patient_returns_404(monkeypatch):
    """Professional A must not target professional B's patient via patientId."""
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.post(
            "/api/v1/ai/speech-analysis",
            json={"patientId": str(patient_b.id), "text": "bla bla bla"},
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404
    assert patient_b.name not in resp.text
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_speech_analysis_own_patient_happy_path(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a2@example.com")
        patient_a = await _make_patient(db, pro_a, name="Paciente A")

    client = await _client(engine, monkeypatch)
    async with client:
        with patch("app.api.v1.ai.run_llm", new=AsyncMock(return_value="análise simulada")):
            resp = await client.post(
                "/api/v1/ai/speech-analysis",
                json={"patientId": str(patient_a.id), "text": "papai mamãe"},
                headers=_auth_headers(pro_a),
            )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "completed"
    assert body["result"] == "análise simulada"
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_transcription_upload_processes_the_selected_audio_file(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db, email="audio@example.com")
        patient = await _make_patient(db, professional, name="Paciente Áudio")

    transcription = SimpleNamespace(
        text="transcrição real da gravação",
        filename="sessao.mp3",
        content_type="audio/mpeg",
        size_bytes=13,
        sha256="a" * 64,
    )
    client = await _client(engine, monkeypatch)
    async with client:
        with patch(
            "app.api.v1.ai.transcribe_audio", new=AsyncMock(return_value=transcription)
        ) as transcribe_mock:
            resp = await client.post(
                "/api/v1/ai/transcribe",
                data={"patientId": str(patient.id)},
                files={"file": ("sessao.mp3", b"ID3-audio-real", "audio/mpeg")},
                headers=_auth_headers(professional),
            )

    assert resp.status_code == 202
    assert resp.json()["result"] == "transcrição real da gravação"
    assert transcribe_mock.await_args.args[0].filename == "sessao.mp3"
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_speech_analysis_audio_uses_transcription_instead_of_sample_text(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db, email="speech-audio@example.com")
        patient = await _make_patient(db, professional, name="Paciente Fala")

    transcription = SimpleNamespace(
        text="papato por sapato",
        filename="fala.wav",
        content_type="audio/wav",
        size_bytes=12,
        sha256="b" * 64,
    )
    client = await _client(engine, monkeypatch)
    async with client:
        with (
            patch("app.api.v1.ai.transcribe_audio", new=AsyncMock(return_value=transcription)),
            patch("app.api.v1.ai.run_llm", new=AsyncMock(return_value="processo fonológico")) as llm,
        ):
            resp = await client.post(
                "/api/v1/ai/speech-analysis/audio",
                data={"patientId": str(patient.id)},
                files={"file": ("fala.wav", b"RIFF-audio", "audio/wav")},
                headers=_auth_headers(professional),
            )

    assert resp.status_code == 202
    assert resp.json()["result"] == "processo fonológico"
    assert "papato por sapato" in llm.await_args.args[0]
    _clear_override()
    await engine.dispose()
