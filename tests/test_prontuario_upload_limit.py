"""Regression test for the prontuario attachment upload size cap (413).

Standalone setup: creates only the tables needed on an in-memory SQLite DB,
mirroring the pattern in test_speech_analysis_ownership.py. `EntitlementMiddleware`
opens its own session directly from `AsyncSessionLocal` (not the FastAPI `get_db`
override), so it is monkeypatched to the same in-memory engine too.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.config import get_settings
from app.core.utils import utcnow
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.timeline import TimelineEvent


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
                    Base.metadata.tables["attachments"],
                    TimelineEvent.__table__,
                ],
            )
        )
    return eng


async def _make_professional(db):
    pro = Professional(
        email_verified_at=utcnow(),
        email="pro-upload@example.com",
        password_hash="x",
        name="Dra. Teste",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990000",
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


async def _make_patient(db, professional):
    p = Patient(
        professional_id=professional.id,
        name="Paciente Upload",
        birth_date=date.today().replace(year=date.today().year - 4),
        diagnosis_keys=["tea"],
        status="ativo",
        start_date=date.today() - timedelta(days=30),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


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
async def test_upload_over_limit_rejected_and_under_limit_succeeds(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 64)

    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db)
        patient = await _make_patient(db, professional)

    headers = {"Authorization": f"Bearer {create_access_token(professional.id)}"}
    client = await _client(engine, monkeypatch)
    async with client:
        with patch("app.api.v1.prontuario.storage_service.upload", new_callable=AsyncMock) as mock_upload:
            oversized = b"x" * 65
            resp = await client.post(
                f"/api/v1/patients/{patient.id}/attachments",
                files={"file": ("laudo.pdf", oversized, "application/pdf")},
                headers=headers,
            )
            assert resp.status_code == 413
            assert "tamanho máximo" in resp.json()["detail"]
            mock_upload.assert_not_called()

            body = b"%PDF-1.4\nconteudo pequeno"
            resp = await client.post(
                f"/api/v1/patients/{patient.id}/attachments",
                files={"file": ("laudo.pdf", body, "application/pdf")},
                headers=headers,
            )
            assert resp.status_code == 201
            assert resp.json()["sizeBytes"] == len(body)
            assert resp.json()["name"] == "laudo.pdf"
            mock_upload.assert_called_once()
            assert mock_upload.await_args.args[2] == "application/pdf"

    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_content_type(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db)
        patient = await _make_patient(db, professional)

    headers = {"Authorization": f"Bearer {create_access_token(professional.id)}"}
    client = await _client(engine, monkeypatch)
    async with client:
        with patch("app.api.v1.prontuario.storage_service.upload", new_callable=AsyncMock) as mock_upload:
            resp = await client.post(
                f"/api/v1/patients/{patient.id}/attachments",
                files={"file": ("evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml")},
                headers=headers,
            )
            assert resp.status_code == 400
            assert "não suportado" in resp.json()["detail"]
            mock_upload.assert_not_called()

    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_rejects_mismatched_magic_bytes(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db)
        patient = await _make_patient(db, professional)

    headers = {"Authorization": f"Bearer {create_access_token(professional.id)}"}
    client = await _client(engine, monkeypatch)
    async with client:
        with patch("app.api.v1.prontuario.storage_service.upload", new_callable=AsyncMock) as mock_upload:
            resp = await client.post(
                f"/api/v1/patients/{patient.id}/attachments",
                files={"file": ("fake.pdf", b"not-a-pdf", "application/pdf")},
                headers=headers,
            )
            assert resp.status_code == 400
            assert "não corresponde" in resp.json()["detail"]
            mock_upload.assert_not_called()

    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_sanitizes_path_traversal_filename(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        professional = await _make_professional(db)
        patient = await _make_patient(db, professional)

    headers = {"Authorization": f"Bearer {create_access_token(professional.id)}"}
    client = await _client(engine, monkeypatch)
    async with client:
        with patch("app.api.v1.prontuario.storage_service.upload", new_callable=AsyncMock) as mock_upload:
            body = b"%PDF-1.4\nok"
            resp = await client.post(
                f"/api/v1/patients/{patient.id}/attachments",
                files={"file": ("../../etc/passwd.pdf", body, "application/pdf")},
                headers=headers,
            )
            assert resp.status_code == 201
            assert resp.json()["name"] == "passwd.pdf"
            key = mock_upload.await_args.args[0]
            assert "../" not in key
            assert "passwd.pdf" in key

    _clear_override()
    await engine.dispose()
