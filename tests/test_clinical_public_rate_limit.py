"""Public clinical rate limiting: hashed identifiers, trusted IP, fail-closed.

F20 consumes this module for the acknowledgement endpoint (10/min per token and
60/min per trusted client IP); F16 reuses the same namespace/parameter contract.
Raw tokens never become Redis keys, and a missing counter store refuses public
mutations (503) instead of failing open. The auth limiter stays untouched.
"""

import hashlib
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.services import clinical_public_rate_limit

RAW_TOKEN = "raw-token-that-must-never-become-a-key"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(*, headers=None, client=("10.0.0.9", 1234)) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/report-deliveries/x/acknowledgement",
            "headers": headers or [],
            "client": client,
            "query_string": b"",
        }
    )


@pytest.fixture
def recorded(monkeypatch):
    """Record (and allow) every counter the limiter asks Redis about."""

    calls: list[dict] = []

    def fake_allow(*, key, max_requests, window_seconds):
        calls.append(
            {"key": key, "max_requests": max_requests, "window_seconds": window_seconds}
        )
        return True

    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", fake_allow)
    return calls


def test_acknowledgement_limits_are_hashed_token_and_trusted_ip(recorded):
    request = _request(headers=[(b"x-forwarded-for", b"203.0.113.10, 198.51.100.1")])
    clinical_public_rate_limit.enforce_report_delivery_acknowledgement_rate_limit(
        request, token_hash=clinical_public_rate_limit.hash_identifier(RAW_TOKEN)
    )

    assert recorded[0] == {
        "key": f"clinical:report-delivery-ack:token:{_sha(RAW_TOKEN)}",
        "max_requests": 10,
        "window_seconds": 60,
    }
    # trusted_proxy_count default (1) skips the immediate proxy, not the client.
    assert recorded[1] == {
        "key": f"clinical:report-delivery-ack:ip:{_sha('203.0.113.10')}",
        "max_requests": 60,
        "window_seconds": 60,
    }
    assert all(RAW_TOKEN not in call["key"] for call in recorded)


def test_leftmost_forwarded_for_hop_is_not_trusted(recorded):
    request = _request(
        headers=[(b"x-forwarded-for", b"6.6.6.6, 203.0.113.10, 198.51.100.1")]
    )
    clinical_public_rate_limit.enforce_report_delivery_acknowledgement_rate_limit(
        request, token_hash=clinical_public_rate_limit.hash_identifier(RAW_TOKEN)
    )

    assert recorded[1]["key"].endswith(_sha("203.0.113.10"))
    assert not recorded[1]["key"].endswith(_sha("6.6.6.6"))


def test_denied_counter_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", lambda **_: False)

    with pytest.raises(HTTPException) as exc:
        clinical_public_rate_limit.enforce_public_rate_limit(
            namespace="clinical:report-delivery-ack:token",
            identifier_hash="a" * 64,
            max_requests=10,
            window_seconds=60,
            detail="Muitas confirmações de recebimento. Tente novamente em instantes.",
            endpoint="report-delivery-acknowledgement",
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
    assert "Muitas confirmações" in exc.value.detail


def test_counter_store_failure_is_fail_closed_503(monkeypatch):
    # The suite runs with AUTH_RATE_LIMIT_FAIL_CLOSED=false: public mutations
    # must still refuse when the counter store is unreachable.
    def _store_down(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", _store_down)

    with pytest.raises(HTTPException) as exc:
        clinical_public_rate_limit.enforce_public_rate_limit(
            namespace="clinical:report-delivery-ack:token",
            identifier_hash="a" * 64,
            max_requests=10,
            window_seconds=60,
            detail="Muitas confirmações de recebimento.",
            endpoint="report-delivery-acknowledgement",
        )

    assert exc.value.status_code == 503
    assert "indisponível" in exc.value.detail


def test_namespace_and_parameters_are_caller_defined(recorded):
    """Reusable surface for future public flows (e.g. F16 home-program check-ins)."""

    clinical_public_rate_limit.enforce_public_rate_limit(
        namespace="clinical:home-program-response:token",
        identifier_hash=clinical_public_rate_limit.hash_identifier("grant-1"),
        max_requests=30,
        window_seconds=900,
        detail="Muitas respostas.",
        endpoint="home-program-response",
    )

    assert recorded == [
        {
            "key": f"clinical:home-program-response:token:{_sha('grant-1')}",
            "max_requests": 30,
            "window_seconds": 900,
        }
    ]


# --------------------------------------------------------------------------- #
# F16 — home-program family responses: 120/min IP, 60/min read, 30/min write
# --------------------------------------------------------------------------- #


def test_home_program_limits_are_trusted_ip_and_hashed_grant(recorded):
    grant_id = str(uuid4())
    grant_hash = clinical_public_rate_limit.hash_identifier(grant_id)
    request = _request(headers=[(b"x-forwarded-for", b"203.0.113.10, 198.51.100.1")])

    clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit(request)
    clinical_public_rate_limit.enforce_home_program_response_read_rate_limit(
        grant_hash=grant_hash
    )
    clinical_public_rate_limit.enforce_home_program_response_ip_rate_limit(request)
    clinical_public_rate_limit.enforce_home_program_response_write_rate_limit(
        grant_hash=grant_hash
    )

    ip_call = {
        "key": f"clinical:home-program-response:ip:{_sha('203.0.113.10')}",
        "max_requests": 120,
        "window_seconds": 60,
    }
    assert recorded[0] == ip_call
    assert recorded[1] == {
        "key": f"clinical:home-program-response:read:{_sha(grant_id)}",
        "max_requests": 60,
        "window_seconds": 60,
    }
    assert recorded[2] == ip_call
    assert recorded[3] == {
        "key": f"clinical:home-program-response:write:{_sha(grant_id)}",
        "max_requests": 30,
        "window_seconds": 60,
    }
    # o identificador do grant nunca aparece cru — só o digest
    assert all(grant_id not in call["key"] for call in recorded)


def test_home_program_upload_limit_is_hashed_grant_with_ten_minute_window(recorded):
    """F16 (5.3): foto tem teto próprio de 10 envios/10 min por grant."""
    grant_id = str(uuid4())
    grant_hash = clinical_public_rate_limit.hash_identifier(grant_id)

    clinical_public_rate_limit.enforce_home_program_response_upload_rate_limit(
        grant_hash=grant_hash
    )

    assert recorded == [
        {
            "key": f"clinical:home-program-response:upload:{_sha(grant_id)}",
            "max_requests": 10,
            "window_seconds": 600,
        }
    ]
    assert grant_id not in recorded[0]["key"]


def test_home_program_upload_limit_denied_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", lambda **_: False)

    with pytest.raises(HTTPException) as exc:
        clinical_public_rate_limit.enforce_home_program_response_upload_rate_limit(
            grant_hash="a" * 64
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "600"
    assert "foto" in exc.value.detail


def test_home_program_write_limit_denied_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", lambda **_: False)

    with pytest.raises(HTTPException) as exc:
        clinical_public_rate_limit.enforce_home_program_response_write_rate_limit(
            grant_hash="a" * 64
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
    assert "Muitas respostas" in exc.value.detail


def test_home_program_rate_limit_fails_closed_without_counter_store(monkeypatch):
    def _store_down(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(clinical_public_rate_limit, "_redis_allow", _store_down)

    with pytest.raises(HTTPException) as exc:
        clinical_public_rate_limit.enforce_home_program_response_write_rate_limit(
            grant_hash="a" * 64
        )

    assert exc.value.status_code == 503
    assert "indisponível" in exc.value.detail
