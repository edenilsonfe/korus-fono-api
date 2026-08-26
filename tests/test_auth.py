import re
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.billing.stub_gateway import StubPaymentGateway
from app.main import app
from app.models.billing import Plan
from app.models.finance import FinancialCategory
from app.models.password_reset_token import (
    PURPOSE_EMAIL_VERIFICATION,
    PasswordResetToken,
)
from app.models.professional import Professional
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
            "phone": "(11) 98888-7777",
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
            "phone": "(11) 98888-7777",
        },
    )
    assert reg.status_code == 201
    _assert_auth_json_has_no_usable_jwt(reg.json())
    assert "korus_access" in reg.cookies
    created_professional = (
        await db_session.execute(select(Professional).where(Professional.email == email))
    ).scalar_one()
    configured_categories = await db_session.execute(
        select(FinancialCategory.name).where(
            FinancialCategory.professional_id == created_professional.id,
            FinancialCategory.kind == "income",
        )
    )
    assert set(configured_categories.scalars()) == {
        "Atendimentos",
        "Avaliações",
        "Pacotes",
        "Taxas de cancelamento",
        "Outras receitas",
    }
    # A sessão recém-criada pode assinar antes da verificação, mas não acessar dados clínicos.
    me = await api_client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["emailVerified"] is False
    assert me.json()["signupPaymentRequired"] is False
    checkout = await api_client.post(
        "/api/v1/billing/checkout",
        json={"planSlug": COMMERCIAL_PLAN_SEEDS[0]["slug"]},
    )
    assert checkout.status_code == 200
    UUID(checkout.json()["sessionId"])
    assert checkout.json()["checkoutUrl"].endswith(
        f"/planos/pagamento?sessionId={checkout.json()['sessionId']}"
    )
    patients_blocked = await api_client.get("/api/v1/patients")
    assert patients_blocked.status_code == 403
    assert patients_blocked.json()["detail"] == "E-mail não verificado"

    assert captured
    verify = await api_client.post("/api/v1/auth/verify-email", json={"token": captured[0]})
    assert verify.status_code == 200

    # Session via HttpOnly cookie — not Authorization Bearer from JSON.
    patients = await api_client.get("/api/v1/patients")
    assert patients.status_code == 200
    demo = next(p for p in patients.json()["items"] if p["name"] == "Paciente demonstração")
    assert demo["isDemo"] is True

    anamnese = await api_client.get(f"/api/v1/patients/{demo['id']}/anamnese")
    assert anamnese.status_code == 200
    entries = {entry["section"]: entry["value"] for entry in anamnese.json()["entries"]}
    assert set(entries) == {
        "Gestação",
        "Parto",
        "Desenvolvimento motor",
        "Desenvolvimento da linguagem",
        "Histórico escolar",
        "Comorbidades",
        "Medicamentos",
        "Observações",
    }
    assert all(value.strip() for value in entries.values())
    assert "fictícios" in entries["Observações"]

    evolutions = await api_client.get(f"/api/v1/patients/{demo['id']}/evolutions")
    assert evolutions.status_code == 200
    evolution_items = evolutions.json()
    assert {item["title"] for item in evolution_items} == {
        "Avaliação inicial",
        "Adaptação ao processo terapêutico",
        "Ampliação da comunicação funcional",
        "Evolução recente",
    }
    assert len(evolution_items) == 4
    assert all(item["content"].strip() for item in evolution_items)

    assessments = await api_client.get(f"/api/v1/patients/{demo['id']}/assessments")
    assert assessments.status_code == 200
    assessment_items = assessments.json()
    assert {item["protocolId"] for item in assessment_items} == {
        "desenvolvimento-infantil",
        "portage",
    }
    assert all(item["status"] == "completed" for item in assessment_items)
    assert all(item["interpretation"].strip() for item in assessment_items)
    assert all(item["metadata"]["demoSeedVersion"] == 1 for item in assessment_items)
    assert "mchat" not in {item["protocolId"] for item in assessment_items}

    goals = await api_client.get(f"/api/v1/patients/{demo['id']}/goals")
    assert goals.status_code == 200
    assert {item["title"] for item in goals.json()} == {
        "Ampliar vocabulário funcional",
        "Combinar duas palavras espontaneamente",
    }

    domains = await api_client.get(f"/api/v1/patients/{demo['id']}/clinical-domains")
    assert domains.status_code == 200
    domain_items = {item["key"]: item for item in domains.json()}
    assert set(domain_items) == {"linguagem", "social", "atencao"}
    assert all(len(item["history"]) == 3 for item in domain_items.values())
    assert all(item["delta"] > 0 for item in domain_items.values())


@pytest.mark.asyncio
async def test_register_checkout_blocks_dashboard_until_payment_is_confirmed(
    api_client, db_session, monkeypatch
):
    monkeypatch.setattr("app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr("app.api.v1.auth.enforce_login_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: StubPaymentGateway())

    async def _noop_welcome(*_args, **_kwargs):
        return None

    async def _empty_dashboard(*_args, **_kwargs):
        return {
            "kpis": {
                "active_patients": 0,
                "new_this_month": 0,
                "sessions_done": 0,
                "sessions_pending": 0,
                "ai_reports": 0,
            },
            "patient_evolution": [],
            "monthly_growth": [],
            "upcoming_appointments": [],
            "protocols_applied": [],
            "today_agenda": [],
            "birthdays_today": [],
            "pending": {
                "evolutions": 0,
                "reports": 0,
                "sessions": 0,
                "assessment_drafts": 0,
                "awaiting_informant": 0,
            },
            "suggestions": [],
        }

    monkeypatch.setattr("app.api.v1.auth.send_whatsapp_welcome_task", _noop_welcome)
    monkeypatch.setattr("app.api.v1.dashboard.build_dashboard", _empty_dashboard)
    captured_tokens: list[str] = []

    def _capture_verification(_to_email, _user_name, raw_token):
        captured_tokens.append(raw_token)

    monkeypatch.setattr(
        "app.api.v1.auth.send_email_verification_email_task",
        _capture_verification,
    )
    monkeypatch.setattr(
        "app.services.saas_billing_service.send_email_verification_email_sync",
        _capture_verification,
    )
    db_session.add(Plan(**COMMERCIAL_PLAN_SEEDS[0]))
    await db_session.commit()

    email = f"checkout-required-{uuid4().hex[:8]}@test.com"
    register = await api_client.post(
        "/api/v1/auth/register-checkout",
        json={
            "email": email,
            "password": "securepass123",
            "name": "Checkout Obrigatório",
            "specialtyKey": "fono",
            "council": "CRFa 1234",
            "phone": "(11) 98888-7777",
            "cpf": "52998224725",
        },
    )
    assert register.status_code == 201
    assert captured_tokens == []
    created_professional = (
        await db_session.execute(select(Professional).where(Professional.email == email))
    ).scalar_one()
    token_before_payment = (
        await db_session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.professional_id == created_professional.id,
                PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
            )
        )
    ).scalar_one_or_none()
    assert token_before_payment is None

    api_client.cookies.clear()
    login_before_payment = await api_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "securepass123"},
    )
    assert login_before_payment.status_code == 200
    assert captured_tokens == []

    me_before_payment = await api_client.get("/api/v1/me")
    assert me_before_payment.status_code == 200
    assert me_before_payment.json()["signupPaymentRequired"] is True

    dashboard_before_checkout = await api_client.get("/api/v1/dashboard")
    assert dashboard_before_checkout.status_code == 403
    assert dashboard_before_checkout.json() == {
        "detail": "Pagamento pendente. Conclua a assinatura para acessar o KorusFono."
    }

    resend_before_payment = await api_client.post("/api/v1/auth/resend-verification")
    assert resend_before_payment.status_code == 403
    assert resend_before_payment.json() == {
        "detail": "Pagamento pendente. Conclua a assinatura para acessar o KorusFono."
    }
    assert captured_tokens == []

    checkout = await api_client.post(
        "/api/v1/billing/checkout",
        json={"planSlug": COMMERCIAL_PLAN_SEEDS[0]["slug"]},
    )
    assert checkout.status_code == 200
    session_id = checkout.json()["sessionId"]
    assert session_id
    assert checkout.json()["accessGranted"] is False

    payment_session_before = await api_client.get(f"/api/v1/billing/checkout/{session_id}")
    assert payment_session_before.status_code == 200
    assert payment_session_before.json()["accessGranted"] is False

    billing_before_payment = await api_client.get("/api/v1/billing/me")
    assert billing_before_payment.status_code == 200
    assert billing_before_payment.json()["signupPaymentRequired"] is True
    assert billing_before_payment.json()["checkoutSessionId"] == session_id

    dashboard_before_payment = await api_client.get("/api/v1/dashboard")
    assert dashboard_before_payment.status_code == 403

    simulated_payment = await api_client.post("/api/v1/billing/reconcile/simulate")
    assert simulated_payment.status_code == 200
    assert simulated_payment.json()["professionalStatus"] == "active"
    assert len(captured_tokens) == 1
    token_after_payment = (
        await db_session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.professional_id == created_professional.id,
                PasswordResetToken.purpose == PURPOSE_EMAIL_VERIFICATION,
            )
        )
    ).scalar_one()
    assert token_after_payment.used_at is None

    me_after_payment = await api_client.get("/api/v1/me")
    assert me_after_payment.status_code == 200
    assert me_after_payment.json()["signupPaymentRequired"] is False

    payment_session_after = await api_client.get(f"/api/v1/billing/checkout/{session_id}")
    assert payment_session_after.status_code == 200
    assert payment_session_after.json()["status"] == "paid"
    assert payment_session_after.json()["accessGranted"] is True

    dashboard_before_verification = await api_client.get("/api/v1/dashboard")
    assert dashboard_before_verification.status_code == 403
    assert dashboard_before_verification.json() == {"detail": "E-mail não verificado"}

    verify = await api_client.post(
        "/api/v1/auth/verify-email",
        json={"token": captured_tokens[0]},
    )
    assert verify.status_code == 200

    dashboard_after_payment = await api_client.get("/api/v1/dashboard")
    assert dashboard_after_payment.status_code == 200
