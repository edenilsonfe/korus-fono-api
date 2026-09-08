import pytest

from app.core.config import Settings, validate_settings

TEST_SECRET = "test-secret-for-pytest-only-not-for-prod"


def test_debug_true_with_default_secret_is_allowed():
    settings = Settings(debug=True, allow_debug=True, jwt_secret="change-me-in-production")
    validate_settings(settings)


def test_debug_false_with_default_secret_raises():
    settings = Settings(debug=False, jwt_secret="change-me-in-production")
    with pytest.raises(RuntimeError):
        validate_settings(settings)


def test_debug_false_with_empty_secret_raises():
    settings = Settings(debug=False, jwt_secret="")
    with pytest.raises(RuntimeError):
        validate_settings(settings)


def test_debug_false_with_short_secret_raises():
    settings = Settings(debug=False, jwt_secret="too-short")
    with pytest.raises(RuntimeError):
        validate_settings(settings)


def test_debug_false_with_strong_secret_is_allowed():
    settings = Settings(
        debug=False,
        frontend_url="https://app.example.com",
        jwt_secret=TEST_SECRET,
        whatsapp_provider="meta",
        billing_provider="asaas",
        asaas_api_key="$aact_test_key_for_jwt_guard",
    )
    validate_settings(settings)
