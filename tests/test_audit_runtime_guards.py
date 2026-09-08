import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.client_ip import get_client_ip
from app.core.config import get_settings
from app.services.ai_service import run_llm


def test_signed_client_ip_rejects_forgery_and_expired_signatures(monkeypatch):
    secret = "audit-shared-secret-for-local-tests-only"
    monkeypatch.setattr("app.core.client_ip.get_settings", lambda: SimpleNamespace(korus_proxy_secret=secret, trusted_proxy_count=0))
    timestamp = str(int(time.time()))
    ip = "203.0.113.8"
    signature = hmac.new(secret.encode(), f"{timestamp}\n{ip}".encode(), hashlib.sha256).hexdigest()

    def request(signature=signature, timestamp=timestamp, ip=ip):
        return Request({"type": "http", "client": ("127.0.0.1", 123), "headers": [(b"x-korus-client-ip", ip.encode()), (b"x-korus-client-time", timestamp.encode()), (b"x-korus-client-signature", signature.encode("latin1"))]})

    assert get_client_ip(request()) == ip
    for forged in [request(signature="0" * 64), request(signature="é" * 64), request(timestamp="0"), request(ip="203.0.113.9"), request(ip="not-an-ip")]:
        assert get_client_ip(forged) == "127.0.0.1"


async def test_llm_timeout_closes_client_and_returns_retryable_error(monkeypatch):
    settings = get_settings().model_copy(update={"opencode_api_key": "fake-key", "assistant_llm_timeout_seconds": 0.01})
    monkeypatch.setattr("app.services.ai_service.get_settings", lambda: settings)

    async def pending(**kwargs):
        await asyncio.sleep(1)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=pending)), close=AsyncMock())
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr("openai.AsyncOpenAI", constructor)
    with pytest.raises(HTTPException) as exc:
        await run_llm("Synthetic prompt")
    assert exc.value.status_code == 503 and exc.value.headers["Retry-After"] == "60"
    assert constructor.call_args.kwargs["max_retries"] == 0
    client.close.assert_awaited_once()


async def test_slow_login_limiter_does_not_block_other_requests(api_client, monkeypatch):
    import threading
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_limiter(*args):
        started.set()
        release.wait(1)
        finished.set()

    monkeypatch.setattr("app.api.v1.auth.enforce_login_rate_limit", slow_limiter)
    login = asyncio.create_task(api_client.post("/api/v1/auth/login", json={"email": "absent@example.com", "password": "fake-password"}))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        health = await api_client.get("/health")
        assert health.status_code == 200
        assert not finished.is_set()
    finally:
        release.set()
        await login
