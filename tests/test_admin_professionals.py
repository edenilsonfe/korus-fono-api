"""Tests for platform admin professional accounts console.

Standalone SQLite setup (only needed tables). Registers JSONB/ARRAY
compilers so related tables used by get_detail can be created.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, String, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.security import create_access_token, hash_password
from app.billing.errors import PaymentGatewayError
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.ai import AIJob
from app.models.assessment import Assessment, ProtocolCatalog
from app.models.billing import Plan, Subscription
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.models.whatsapp_connection import WhatsAppConnection
from app.services.admin_professional_service import (
    AdminBillingSyncError,
    AdminProfessionalService,
)

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
        Session.__table__,
        Assessment.__table__,
        WhatsAppConnection.__table__,
        AIJob.__table__,
        Plan.__table__,
        Subscription.__table__,
    ]
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(bind=sync_conn, tables=tables))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


async def _make_professional(db, *, email, is_staff=False, password="testpass123", **kwargs):
    pro = Professional(
        email=email,
        password_hash=hash_password(password),
        name=f"Pro {email}",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990000",
        cpf="12345678901",
        is_staff=is_staff,
        subscription_status=kwargs.get("subscription_status", "trialing"),
        trial_ends_at=kwargs.get(
            "trial_ends_at",
            datetime.now(timezone.utc) + timedelta(days=3),
        ),
        is_disabled=kwargs.get("is_disabled", False),
        token_version=kwargs.get("token_version", 0),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


def _auth_headers(professional: Professional):
    return {
        "Authorization": f"Bearer {create_access_token(professional.id, professional.token_version)}"
    }


async def _client(db):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clear_override():
    app.dependency_overrides.clear()


async def test_non_staff_forbidden(db):
    pro = await _make_professional(db, email="user@x.com", is_staff=False)
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/professionals", headers=_auth_headers(pro))
    assert resp.status_code == 403
    _clear_override()


async def test_staff_list_and_detail(db):
    staff = await _make_professional(db, email="staff@x.com", is_staff=True)
    target = await _make_professional(db, email="target@x.com")
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/professionals", headers=_auth_headers(staff))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 2
        assert any(i["email"] == "target@x.com" for i in body["items"])

        resp = await client.get(
            f"/api/v1/admin/professionals/{target.id}", headers=_auth_headers(staff)
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["email"] == "target@x.com"
        assert detail["cpfMasked"] == "***.***.***-01"
        assert "passwordHash" not in detail
        assert detail["counts"]["patients"] == 0
    _clear_override()


async def test_extend_trial_and_audit(db):
    staff = await _make_professional(db, email="staff2@x.com", is_staff=True)
    target = await _make_professional(
        db,
        email="expired@x.com",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/admin/professionals/{target.id}/extend-trial",
            headers=_auth_headers(staff),
            json={"days": 7, "reason": "suporte"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["subscriptionStatus"] == "trialing"
    result = await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "extend_trial"))
    log = result.scalar_one()
    assert log.actor_id == staff.id
    assert log.target_professional_id == target.id
    assert log.payload["days"] == 7
    _clear_override()


async def test_disable_blocks_login(db):
    staff = await _make_professional(db, email="staff3@x.com", is_staff=True)
    target = await _make_professional(db, email="disableme@x.com", password="secret123")
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/admin/professionals/{target.id}/disable",
            headers=_auth_headers(staff),
            json={"reason": "abuso"},
        )
        assert resp.status_code == 200
        assert resp.json()["isDisabled"] is True

        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "disableme@x.com", "password": "secret123"},
        )
        assert resp.status_code == 401
    _clear_override()


async def test_disable_cancels_active_asaas_recurring_subscription(db):
    staff = await _make_professional(db, email="staff-billing@x.com", is_staff=True)
    target = await _make_professional(
        db,
        email="disable-billing@x.com",
        subscription_status="active",
    )
    plan = Plan(
        slug="disable-monthly",
        name="Plano mensal",
        price_cents=9790,
        billing_interval="monthly",
    )
    db.add(plan)
    await db.flush()
    subscription = Subscription(
        professional_id=target.id,
        plan_id=plan.id,
        status="active",
        provider="asaas",
        external_subscription_id="sub_disabled_account",
    )
    db.add(subscription)
    await db.commit()

    gateway = AsyncMock()
    gateway.cancel_subscription = AsyncMock(return_value={"status": "canceled"})
    with patch(
        "app.services.admin_professional_service.AsaasPaymentGateway",
        return_value=gateway,
        create=True,
    ):
        detail = await AdminProfessionalService(db).disable(
            actor=staff,
            professional_id=target.id,
            reason="encerramento da conta",
        )

    assert detail.is_disabled is True
    assert detail.subscription_status == "canceled"
    gateway.cancel_subscription.assert_awaited_once_with(
        external_subscription_id="sub_disabled_account"
    )
    await db.refresh(subscription)
    assert subscription.status == "canceled"
    _clear_override()


async def test_disable_stops_when_asaas_recurring_cancellation_fails(db):
    staff = await _make_professional(db, email="staff-sync-failure@x.com", is_staff=True)
    target = await _make_professional(
        db,
        email="disable-sync-failure@x.com",
        subscription_status="active",
    )
    plan = Plan(
        slug="disable-sync-failure-monthly",
        name="Plano mensal",
        price_cents=9790,
        billing_interval="monthly",
    )
    db.add(plan)
    await db.flush()
    subscription = Subscription(
        professional_id=target.id,
        plan_id=plan.id,
        status="active",
        provider="asaas",
        external_subscription_id="sub_sync_failure",
    )
    db.add(subscription)
    await db.commit()

    gateway = AsyncMock()
    gateway.cancel_subscription = AsyncMock(
        side_effect=PaymentGatewayError("Asaas indisponível", status_code=503)
    )
    with (
        patch(
            "app.services.admin_professional_service.AsaasPaymentGateway",
            return_value=gateway,
        ),
        pytest.raises(AdminBillingSyncError),
    ):
        await AdminProfessionalService(db).disable(
            actor=staff,
            professional_id=target.id,
            reason="encerramento da conta",
        )

    await db.refresh(target)
    await db.refresh(subscription)
    assert target.is_disabled is False
    assert target.subscription_status == "active"
    assert subscription.status == "active"


async def test_self_disable_conflict(db):
    staff = await _make_professional(db, email="staff4@x.com", is_staff=True)
    client = await _client(db)
    async with client:
        resp = await client.post(
            f"/api/v1/admin/professionals/{staff.id}/disable",
            headers=_auth_headers(staff),
            json={"reason": "Teste de proteção contra autodesativação"},
        )
        assert resp.status_code == 409
    _clear_override()


async def test_invalidate_sessions_rejects_old_refresh(db):
    staff = await _make_professional(db, email="staff5@x.com", is_staff=True)
    target = await _make_professional(db, email="sessions@x.com")
    client = await _client(db)
    async with client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "sessions@x.com", "password": "testpass123"},
        )
        assert login.status_code == 200
        old_refresh = login.cookies.get("korus_refresh")
        assert old_refresh

        resp = await client.post(
            f"/api/v1/admin/professionals/{target.id}/invalidate-sessions",
            headers=_auth_headers(staff),
            json={"reason": "Teste de invalidação das sessões"},
        )
        assert resp.status_code == 200

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refreshToken": old_refresh},
        )
        assert resp.status_code == 401
    _clear_override()


async def test_hub_stats(db):
    staff = await _make_professional(db, email="s@x.com", is_staff=True)
    client = await _client(db)
    async with client:
        resp = await client.get("/api/v1/admin/stats", headers=_auth_headers(staff))
        assert resp.status_code == 200
        body = resp.json()
        assert body["staff"] >= 1
        assert "trialing" in body
    _clear_override()
