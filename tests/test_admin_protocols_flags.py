"""Tests for admin protocols publication + feature flags."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.assessment import Assessment, ProtocolCatalog
from app.models.feature_flag import FeatureFlag, FeatureFlagOverride
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.feature_flag_service import FeatureFlagService

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "TEXT"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    tables = [
        Professional.__table__,
        AdminAuditLog.__table__,
        ProtocolCatalog.__table__,
        Patient.__table__,
        Assessment.__table__,
        FeatureFlag.__table__,
        FeatureFlagOverride.__table__,
    ]
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(bind=sync_conn, tables=tables))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProtocolCatalog(
                id="abfw",
                name="ABFW",
                full_name="ABFW Full",
                description="desc",
                age_range="2-12",
                field_templates=[],
                is_active=True,
                sort_order=1,
            )
        )
        session.add(
            ProtocolCatalog(
                id="hidden",
                name="Hidden",
                full_name="Hidden Full",
                description="desc",
                age_range="0-1",
                field_templates=[],
                is_active=False,
                sort_order=99,
            )
        )
        session.add(
            FeatureFlag(key="ai_assistant", description="IA", enabled_global=True)
        )
        session.add(
            FeatureFlag(key="demo_protocol", description="Demo", enabled_global=False)
        )
        await session.commit()
        yield session


async def _make_professional(db, *, email, is_staff=False, specialty_key="fono"):
    pro = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=f"Pro {email}",
        specialty_key=specialty_key,
        specialty="Fono",
        council="CRFa",
        phone="11999990000",
        is_staff=is_staff,
        subscription_status="active",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


def _auth(pro: Professional):
    return {"Authorization": f"Bearer {create_access_token(pro.id, pro.token_version)}"}


async def _client(db):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear():
    app.dependency_overrides.clear()


async def test_public_protocols_omit_inactive(db):
    pro = await _make_professional(db, email="clin@x.com")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/protocols", headers=_auth(pro))
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert "abfw" in ids
        assert "hidden" not in ids
    _clear()


async def test_admin_can_see_and_activate_protocol(db):
    staff = await _make_professional(db, email="staff@x.com", is_staff=True)
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/protocols", headers=_auth(staff))
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()]
        assert "hidden" in ids

        resp = await client.patch(
            "/api/v1/admin/protocols/hidden",
            headers=_auth(staff),
            json={"isActive": True, "fidelityBadge": "DEV-SAMPLE", "sortOrder": 2},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["isActive"] is True
        assert body["fidelityBadge"] == "DEV-SAMPLE"
        assert body["sortOrder"] == 2
    _clear()


async def test_unknown_flag_fail_closed(db):
    pro = await _make_professional(db, email="u@x.com")
    service = FeatureFlagService(db)
    assert await service.is_enabled(pro, "does_not_exist") is False
    assert await service.is_enabled(pro, "ai_assistant") is True
    assert await service.is_enabled(pro, "demo_protocol") is False


async def test_override_forces_flag(db):
    staff = await _make_professional(db, email="staff2@x.com", is_staff=True)
    target = await _make_professional(db, email="t@x.com")
    client = await _client(db)
    async with client:
        resp = await client.put(
            f"/api/v1/admin/professionals/{target.id}/feature-flags/demo_protocol",
            headers=_auth(staff),
            json={"enabled": True, "reason": "beta"},
        )
        assert resp.status_code == 200
        states = {s["key"]: s for s in resp.json()}
        assert states["demo_protocol"]["override"] is True
        assert states["demo_protocol"]["resolved"] is True

        resp = await client.put(
            f"/api/v1/admin/professionals/{target.id}/feature-flags/demo_protocol",
            headers=_auth(staff),
            json={"enabled": None},
        )
        assert resp.status_code == 200
        states = {s["key"]: s for s in resp.json()}
        assert states["demo_protocol"]["override"] is None
        assert states["demo_protocol"]["resolved"] is False
    _clear()


async def test_non_staff_forbidden_admin_protocols(db):
    pro = await _make_professional(db, email="ns@x.com")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/protocols", headers=_auth(pro))
        assert resp.status_code == 403
    _clear()
