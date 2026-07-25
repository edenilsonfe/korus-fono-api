import os
import uuid
from datetime import UTC, date, datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test-secret-for-pytest-only-not-for-prod")
os.environ.setdefault("AUTH_RATE_LIMIT_FAIL_CLOSED", "false")
# Worktree/CI sem .env: app sobe em modo debug (exige ALLOW_DEBUG).
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("ALLOW_DEBUG", "true")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.security import create_access_token, hash_password
from app.db.base import Base
import app.models  # noqa: F401
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
                )
            )
        await session.commit()
        yield session


@pytest.fixture
async def professional(db_session: AsyncSession):
    pro = Professional(
        email="spm-test@example.com",
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
async def api_client(db_session: AsyncSession):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_spm_cache():
    from app.services.spm_content_package import get_spm_content_package

    get_spm_content_package.cache_clear()
    yield
    get_spm_content_package.cache_clear()


@pytest.fixture(autouse=True)
def clear_instrument_cache():
    from app.services.instrument_content_package import clear_instrument_content_package_cache

    clear_instrument_content_package_cache()
    yield
    clear_instrument_content_package_cache()
