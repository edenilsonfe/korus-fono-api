import re
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.billing.stub_gateway import StubPaymentGateway
from app.main import app
from app.models.billing import Plan
from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS

# JWT-shaped values (three base64url segments) must not appear in auth JSON.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _assert_auth_json_has_no_usable_jwt(data: dict) -> None:
    assert data.get("accessToken", "") == ""
    assert data.get("refreshToken", "") == ""
    assert data.get("tokenType") == "bearer"
    assert _JWT_RE.search(str(data)) is None


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_register_and_login(client):
    email = "newuser@test.com"
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "securepass123",
            "name": "Test User",
            "specialtyKey": "fono",
            "council": "CRFa",
            "cpf": "52998224725",
        },
    )
    if reg.status_code == 201:
        _assert_auth_json_has_no_usable_jwt(reg.json())
        assert "korus_access" in reg.cookies
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "securepass123"})
    if login.status_code == 200:
        _assert_auth_json_has_no_usable_jwt(login.json())
        assert "korus_access" in login.cookies


@pytest.mark.asyncio
async def test_register_can_checkout_before_verification_and_creates_demo_patient(
    api_client, db_session, monkeypatch
):
    # Avoid shared in-memory rate-limit state from other auth tests in the same run.
    monkeypatch.setattr("app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None)
    captured: list[str] = []

    def _capture_send(to_email, user_name, raw_token):
        captured.append(raw_token)

    monkeypatch.setattr("app.api.v1.auth.send_email_verification_email_task", _capture_send)
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: StubPaymentGateway())
    db_session.add(Plan(**COMMERCIAL_PLAN_SEEDS[0]))
    await db_session.commit()

    email = f"nocpf-{uuid4().hex[:8]}@test.com"
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "securepass123",
            "name": "Sem Cpf",
            "specialtyKey": "fono",
        },
    )
    assert reg.status_code == 201
    _assert_auth_json_has_no_usable_jwt(reg.json())
    assert "korus_access" in reg.cookies
    # A sessão recém-criada pode assinar antes da verificação, mas não acessar dados clínicos.
    me = await api_client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["emailVerified"] is False
    checkout = await api_client.post(
        "/api/v1/billing/checkout",
        json={"planSlug": COMMERCIAL_PLAN_SEEDS[0]["slug"]},
    )
    assert checkout.status_code == 200
    assert checkout.json()["sessionId"].startswith("stub_pay_")
    patients_blocked = await api_client.get("/api/v1/patients")
    assert patients_blocked.status_code == 403
    assert patients_blocked.json()["detail"] == "E-mail não verificado"

    assert captured
    verify = await api_client.post("/api/v1/auth/verify-email", json={"token": captured[0]})
    assert verify.status_code == 200

    # Session via HttpOnly cookie — not Authorization Bearer from JSON.
    patients = await api_client.get("/api/v1/patients")
    assert patients.status_code == 200
    names = [p["name"] for p in patients.json()["items"]]
    assert "Paciente demonstração" in names
