import pytest

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
