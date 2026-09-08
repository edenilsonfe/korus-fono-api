import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import create_app

TEST_SECRET = "test-secret-for-pytest-only-not-for-prod"


def _make_settings(*, debug: bool) -> Settings:
    return Settings(
        debug=debug,
        frontend_url="https://app.example.com",
        jwt_secret=TEST_SECRET,
        whatsapp_provider="meta",
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_docs_disabled_when_debug_false(monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: _make_settings(debug=False))
    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/redoc")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


@pytest.mark.asyncio
async def test_docs_available_when_debug_true(monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: _make_settings(debug=True))
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/docs")
        assert response.status_code == 200
