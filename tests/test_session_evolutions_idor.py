"""IDOR regression test for GET /patients/{patient_id}/sessions/{session_id}/evolutions.

Standalone setup: creates only the tables needed on an in-memory SQLite DB,
mirroring the pattern in test_assistant.py.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.utils import utcnow
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.evolution import Evolution
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session

# Patient.diagnosis_keys (JSONB) and Session.objectives (ARRAY) are Postgres
# types SQLAlchemy 2.x can't render for SQLite CREATE TABLE. Map them to
# plain JSON for this in-memory test DB only.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element, compiler, **kw):
    return "JSON"


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
                    Patient.__table__,
                    Session.__table__,
                    Evolution.__table__,
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


async def _make_professional(db, *, email):
    pro = Professional(
        email_verified_at=utcnow(),
        email=email,
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


async def _make_session(db, patient, professional):
    # Session.objectives is a Postgres ARRAY column; SQLAlchemy 2.x has no
    # bind processor for it on SQLite, so insert via raw SQL (JSON text)
    # instead of the ORM to avoid the same limitation as the DDL above.
    session_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            "INSERT INTO sessions "
            "(id, patient_id, professional_id, date, duration, type, objectives, notes, created_at, updated_at) "
            "VALUES (:id, :patient_id, :professional_id, :date, :duration, :type, :objectives, :notes, :created_at, :updated_at)"
        ),
        {
            # postgresql.UUID's bind processor renders as unhyphenated hex on
            # non-native dialects (SQLite); match it so later ORM lookups by
            # id (which go through the same processor) can find this row.
            "id": session_id.hex,
            "patient_id": patient.id.hex,
            "professional_id": professional.id.hex,
            "date": now.isoformat(),
            "duration": 45,
            "type": "atendimento",
            "objectives": json.dumps([]),
            "notes": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    await db.commit()
    return SimpleNamespace(id=session_id)


async def _make_evolution(db, patient, session, professional, *, content):
    e = Evolution(
        patient_id=patient.id,
        session_id=session.id,
        professional_id=professional.id,
        date=datetime.now(timezone.utc),
        title="Evolução",
        content=content,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


def _auth_headers(professional):
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


async def _client(db):
    factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear_override():
    app.dependency_overrides.pop(get_db, None)


async def test_session_evolutions_idor_cross_tenant_returns_404(db):
    """Professional A must not read professional B's evolutions via B's session id."""
    pro_a = await _make_professional(db, email="pro-a@example.com")
    pro_b = await _make_professional(db, email="pro-b@example.com")
    patient_a = await _make_patient(db, pro_a, name="Paciente A")
    patient_b = await _make_patient(db, pro_b, name="Paciente B")
    session_b = await _make_session(db, patient_b, pro_b)
    await _make_evolution(db, patient_b, session_b, pro_b, content="Conteúdo sigiloso de B")

    client = await _client(db)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_a.id}/sessions/{session_b.id}/evolutions",
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404
    _clear_override()


async def test_session_evolutions_happy_path_returns_own_evolution(db):
    pro_a = await _make_professional(db, email="pro-a2@example.com")
    patient_a = await _make_patient(db, pro_a, name="Paciente A")
    session_a = await _make_session(db, patient_a, pro_a)
    await _make_evolution(db, patient_a, session_a, pro_a, content="Conteúdo de A")

    client = await _client(db)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_a.id}/sessions/{session_a.id}/evolutions",
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["content"] == "Conteúdo de A"
    _clear_override()
