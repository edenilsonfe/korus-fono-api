"""HTTP multi-tenant isolation matrix: professional A must never read, write, or
discover professional B's patients, appointments, timeline events, or session
evolutions through any documented route.

Standalone setup: creates only the tables needed on an in-memory SQLite DB,
mirroring the pattern in test_session_evolutions_idor.py /
test_speech_analysis_ownership.py. `EntitlementMiddleware` opens its own
session directly from `AsyncSessionLocal` (not the FastAPI `get_db` override),
so it is monkeypatched to the same in-memory engine for every non-GET request.
"""

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
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
from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.care_team import PatientCareTeamMember
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session as ClinicalSession
from app.models.timeline import TimelineEvent


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


async def _engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    FeatureFlag.__table__,
                    Patient.__table__,
                    Caregiver.__table__,
                    ClinicalSession.__table__,
                    Goal.__table__,
                    Assessment.__table__,
                    Evolution.__table__,
                    Appointment.__table__,
                    TimelineEvent.__table__,
                    PatientCareTeamMember.__table__,
                ],
            )
        )
    return eng


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


async def _make_appointment(db, patient, professional, *, appt_date):
    a = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=appt_date,
        time=time(9, 0),
        type="atendimento",
        duration=45,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _make_timeline_event(db, patient, professional, *, title):
    e = TimelineEvent(
        patient_id=patient.id,
        professional_id=professional.id,
        type="sessao",
        title=title,
        date=datetime.now(timezone.utc),
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


def _auth_headers(professional, *, token_version=0):
    return {"Authorization": f"Bearer {create_access_token(professional.id, token_version)}"}


async def _client(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    # EntitlementMiddleware bypasses the get_db override and opens its own
    # session from AsyncSessionLocal on every non-GET request — point it at
    # the same test engine.
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear_override():
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_patient_cross_tenant_returns_404(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_b.id}",
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404
    assert patient_b.name not in resp.text
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_patch_patient_cross_tenant_returns_404(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.patch(
            f"/api/v1/patients/{patient_b.id}",
            json={"name": "Nome Adulterado"},
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404

    async with factory() as db:
        refreshed = await db.get(Patient, patient_b.id)
        assert refreshed.name == "Paciente Confidencial B"
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_patient_assessments_cross_tenant_returns_404(monkeypatch):
    """Discover path: A must not even confirm B's patient id exists via /assessments."""
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_b.id}/assessments",
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_appointments_excludes_other_tenant(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    today = date.today()
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_a = await _make_patient(db, pro_a, name="Paciente A")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")
        appt_a = await _make_appointment(db, patient_a, pro_a, appt_date=today)
        appt_b = await _make_appointment(db, patient_b, pro_b, appt_date=today)

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get(
            "/api/v1/appointments",
            params={"from": (today - timedelta(days=1)).isoformat(), "to": (today + timedelta(days=1)).isoformat()},
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body}
    assert str(appt_a.id) in ids
    assert str(appt_b.id) not in ids
    assert patient_b.name not in resp.text
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_global_timeline_excludes_other_tenant(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_a = await _make_patient(db, pro_a, name="Paciente A")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")
        event_a = await _make_timeline_event(db, patient_a, pro_a, title="Evento de A")
        event_b = await _make_timeline_event(db, patient_b, pro_b, title="Evento sigiloso de B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get("/api/v1/timeline", headers=_auth_headers(pro_a))
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body}
    assert str(event_a.id) in ids
    assert str(event_b.id) not in ids
    assert event_b.title not in resp.text
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_session_evolutions_cross_tenant_returns_404(monkeypatch):
    """Professional A must not read professional B's evolutions via B's session id."""
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        pro_b = await _make_professional(db, email="pro-b@example.com")
        patient_a = await _make_patient(db, pro_a, name="Paciente A")
        patient_b = await _make_patient(db, pro_b, name="Paciente Confidencial B")
        session_b = await _make_session(db, patient_b, pro_b)
        await _make_evolution(db, patient_b, session_b, pro_b, content="Conteúdo sigiloso de B")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_a.id}/sessions/{session_b.id}/evolutions",
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 404
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_own_patient_happy_path_returns_200(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")
        patient_a = await _make_patient(db, pro_a, name="Paciente A")

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get(
            f"/api/v1/patients/{patient_a.id}",
            params={"include": "goals"},
            headers=_auth_headers(pro_a),
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Paciente A"
    _clear_override()
    await engine.dispose()


@pytest.mark.asyncio
async def test_revoked_token_version_returns_401_on_me(monkeypatch):
    """Password reset / logout-all bumps token_version; old access tokens must be rejected."""
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pro_a = await _make_professional(db, email="pro-a@example.com")

    token = create_access_token(pro_a.id, token_version=0)

    async with factory() as db:
        refreshed = await db.get(Professional, pro_a.id)
        refreshed.token_version = 1
        await db.commit()

    client = await _client(engine, monkeypatch)
    async with client:
        resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    _clear_override()
    await engine.dispose()
