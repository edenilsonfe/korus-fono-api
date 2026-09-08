import os
import uuid
from datetime import UTC, date, datetime, timedelta
from contextlib import asynccontextmanager

os.environ.setdefault("JWT_SECRET", "test-secret-for-pytest-only-not-for-prod")
os.environ["AUTH_RATE_LIMIT_FAIL_CLOSED"] = "false"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
# Worktree/CI sem .env: app sobe em modo debug (exige ALLOW_DEBUG).
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("ALLOW_DEBUG", "true")

import pytest
from app.core.config import Settings
Settings.model_config["env_file"] = None
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import JSON

from app.core.security import create_access_token, hash_password
from app.db.base import Base
import app.models  # noqa: F401
for table in Base.metadata.tables.values():
    for column in table.columns:
        if isinstance(column.type, ARRAY):
            column.type = column.type.with_variant(JSON(), "sqlite")
from app.db.session import get_db
from app.main import app
from app.models.assessment import ProtocolCatalog
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional
from app.seeds.protocols import PROTOCOLS

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        for protocol in PROTOCOLS:
            session.add(
                ProtocolCatalog(
                    id=protocol["id"],
                    name=protocol["name"],
                    full_name=protocol["full_name"],
                    description=protocol["description"],
                    age_range=protocol["age_range"],
                    field_templates=protocol.get("field_templates", []),
                    is_active=protocol.get("is_active", True),
                )
            )
        await session.commit()
        yield session


@pytest.fixture
async def professional(db_session: AsyncSession):
    pro = Professional(
        email="protocol-test@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Teste",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(pro)
    await db_session.commit()
    await db_session.refresh(pro)
    return pro


@pytest.fixture
async def patient(db_session: AsyncSession, professional: Professional):
    patient = Patient(
        professional_id=professional.id,
        name="João Silva",
        birth_date=date.today().replace(year=date.today().year - 4),
        diagnosis_keys=["tea"],
        status="ativo",
        start_date=date.today() - timedelta(days=30),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.flush()
    db_session.add(
        Caregiver(
            patient_id=patient.id,
            name="Maria Silva",
            relation="Mãe",
            phone="11988887777",
            is_primary=True,
        )
    )
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


@pytest.fixture
def auth_headers(professional: Professional):
    token = create_access_token(professional.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def api_client(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False))
    async def override_get_db():
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            # Test code retains fixture objects across rejected requests. Reload
            # them explicitly instead of leaving async lazy loads on attributes.
            for instance in list(db_session.identity_map.values()):
                from sqlalchemy.exc import InvalidRequestError
                try:
                    await db_session.refresh(instance)
                except InvalidRequestError:
                    # A row inserted during the rejected request was rolled back.
                    db_session.expunge(instance)
            raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_instrument_cache():
    from app.services.instrument_content_package import clear_instrument_content_package_cache

    clear_instrument_content_package_cache()
    yield
    clear_instrument_content_package_cache()


@pytest.fixture(autouse=True)
def isolate_entitlement_database(monkeypatch):
    from app.middleware import entitlement
    original = entitlement.AsyncSessionLocal

    @asynccontextmanager
    async def test_session():
        override = app.dependency_overrides.get(get_db)
        if override is None:
            async with original() as session:
                yield session
        else:
            generator = override()
            try:
                yield await anext(generator)
            finally:
                await generator.aclose()

    monkeypatch.setattr(entitlement, "AsyncSessionLocal", test_session)


@pytest.fixture(autouse=True)
def isolate_auth_rate_limit(monkeypatch):
    # Endpoint limit tests replace this with their deterministic allow/deny case.
    # Ordinary domain tests must not depend on a shared local Redis instance.
    monkeypatch.setattr("app.services.auth_rate_limit._redis_allow", lambda **_: True)


@pytest.fixture
async def audit_pg_factory():
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    url = os.getenv("TEST_AUDIT_PG_URL") or os.getenv("TEST_AFFILIATE_PG_URL")
    if not url:
        pytest.skip("Set TEST_AUDIT_PG_URL to a disposable local korus_audit database")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.database == "korus_audit", "Only the disposable local audit database is allowed"
    schema = "audit_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema, "statement_timeout": "10000"}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
