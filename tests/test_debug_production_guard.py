import pytest
from pydantic import ValidationError

from app.core.config import Settings, is_production_runtime, validate_settings

TEST_SECRET = "test-secret-for-pytest-only-not-for-prod"
ASAAS_KEY = "$aact_test_key_not_real_but_looks_like_one"
EVOLUTION_KW = {
    "whatsapp_provider": "evolution",
    "evolution_global_api_key": "evo-key",
    "evolution_webhook_secret": "evo-secret",
    "whatsapp_credential_encryption_key": "cred-key-32chars-minimum!!!!!!",
    "app_public_url": "https://api.example.com",
}


def _prod_asaas(**kwargs) -> Settings:
    base = {
        "debug": False,
        "allow_debug": False,
        "sentry_environment": "production",
        "jwt_secret": TEST_SECRET,
        "billing_provider": "asaas",
        "asaas_api_key": ASAAS_KEY,
        "opencode_api_key": "opencode-test-key",
        "frontend_url": "https://app.korusfono.com.br",
        **EVOLUTION_KW,
    }
    base.update(kwargs)
    return Settings(**base)


def test_is_production_runtime_for_production_and_prod():
    assert is_production_runtime(Settings(sentry_environment="production"))
    assert is_production_runtime(Settings(sentry_environment="prod"))
    assert is_production_runtime(Settings(sentry_environment="PRODUCTION"))
    assert not is_production_runtime(Settings(sentry_environment=""))
    assert not is_production_runtime(Settings(sentry_environment="development"))
    assert not is_production_runtime(Settings(sentry_environment="staging"))


def test_debug_true_without_allow_debug_raises():
    settings = Settings(debug=True, allow_debug=False, sentry_environment="development")
    with pytest.raises(RuntimeError, match="ALLOW_DEBUG"):
        validate_settings(settings)


def test_debug_true_with_allow_debug_is_allowed_outside_prod():
    settings = Settings(debug=True, allow_debug=True, sentry_environment="")
    validate_settings(settings)


def test_production_refuses_debug_even_with_allow_debug():
    settings = _prod_asaas(debug=True, allow_debug=True)
    with pytest.raises(RuntimeError, match="DEBUG=true"):
        validate_settings(settings)


def test_production_refuses_billing_stub():
    settings = _prod_asaas(billing_provider="stub", asaas_api_key="")
    with pytest.raises(RuntimeError, match="stub"):
        validate_settings(settings)


def test_production_refuses_asaas_without_key_as_stub():
    settings = _prod_asaas(billing_provider="asaas", asaas_api_key="")
    with pytest.raises(RuntimeError, match="stub"):
        validate_settings(settings)


def test_production_allows_asaas():
    validate_settings(_prod_asaas())


def test_production_refuses_unconfigured_ai_provider():
    settings = _prod_asaas(opencode_api_key="")
    with pytest.raises(RuntimeError, match="OPENCODE_API_KEY"):
        validate_settings(settings)


@pytest.mark.parametrize(
    "frontend_url",
    [
        "",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://[::1]:5173",
    ],
)
def test_production_refuses_non_public_frontend_url(frontend_url: str):
    settings = _prod_asaas(frontend_url=frontend_url)
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        validate_settings(settings)


def test_non_debug_runtime_refuses_local_frontend_without_sentry_environment():
    settings = _prod_asaas(
        sentry_environment="",
        frontend_url="http://localhost:5173",
    )
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        validate_settings(settings)


def test_local_stub_billing_still_allowed():
    settings = Settings(
        debug=True,
        allow_debug=True,
        sentry_environment="development",
        billing_provider="stub",
    )
    validate_settings(settings)


@pytest.mark.parametrize("provider", ["assas", "stripe", "", "   "])
def test_unknown_billing_provider_is_rejected(provider):
    with pytest.raises(ValidationError, match="BILLING_PROVIDER"):
        _prod_asaas(billing_provider=provider)


def test_billing_provider_normalizes_known_values():
    assert _prod_asaas(billing_provider=" ASAAS ").effective_billing_provider == "asaas"


async def test_invalid_gateway_returns_503_without_persisting_checkout(
    api_client, auth_headers, db_session, monkeypatch,
):
    from sqlalchemy import func, select
    from app.core.config import get_settings
    from app.models.billing import BillingCustomer, Plan, Subscription
    from app.services.plan_catalog_seed import COMMERCIAL_PLAN_SEEDS

    plan = Plan(**COMMERCIAL_PLAN_SEEDS[1])
    db_session.add(plan)
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "billing_provider", "assas")
    response = await api_client.post("/api/v1/billing/checkout", headers=auth_headers,
                                    json={"planSlug": plan.slug})
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BillingCustomer)) == 0


@pytest.mark.parametrize("provider", ["assas", ""])
def test_explicit_unknown_gateway_never_falls_back(provider):
    from app.billing import PaymentGatewayConfigError, get_payment_gateway
    with pytest.raises(PaymentGatewayConfigError):
        get_payment_gateway(provider)
