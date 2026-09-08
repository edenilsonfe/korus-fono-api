from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _redis_allow(key: str, max_requests: int, window_seconds: int) -> bool:
    import redis

    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        current, _ = pipe.execute()
        return int(current) <= max_requests
    finally:
        client.close()


def _enforce_rate_limit(
    *,
    key: str,
    max_requests: int,
    window_seconds: int,
    retry_after: str,
    detail: str,
    endpoint: str,
) -> None:
    try:
        allowed = _redis_allow(key=key, max_requests=max_requests, window_seconds=window_seconds)
    except Exception as exc:
        settings = get_settings()
        if settings.auth_rate_limit_fail_closed:
            logger.error("%s rate limit unavailable (fail-closed): %s", endpoint, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Serviço temporariamente indisponível. Tente novamente em instantes.",
            ) from exc
        logger.warning("%s rate limit unavailable (fail-open): %s", endpoint, exc)
        return
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": retry_after},
        )


def enforce_login_rate_limit(ip: str, email: str) -> None:
    normalized = _normalize_email(email)
    _enforce_rate_limit(
        key=f"auth:login:{ip}:{normalized}",
        max_requests=10,
        window_seconds=900,
        retry_after="900",
        detail="Muitas tentativas de login. Tente novamente mais tarde.",
        endpoint="login",
    )


def enforce_register_rate_limit(ip: str) -> None:
    _enforce_rate_limit(
        key=f"auth:register:{ip}",
        max_requests=5,
        window_seconds=3600,
        retry_after="3600",
        detail="Muitas tentativas de cadastro. Tente novamente mais tarde.",
        endpoint="register",
    )


def enforce_forgot_rate_limit(ip: str, email: str) -> None:
    _enforce_rate_limit(
        key=f"auth:forgot:{ip}:{_normalize_email(email)}",
        max_requests=3,
        window_seconds=3600,
        retry_after="3600",
        detail="Muitas tentativas. Tente novamente mais tarde.",
        endpoint="forgot-password",
    )


def enforce_reset_rate_limit(ip: str) -> None:
    _enforce_rate_limit(
        key=f"auth:reset:{ip}",
        max_requests=10,
        window_seconds=60,
        retry_after="60",
        detail="Muitas tentativas. Tente novamente em instantes.",
        endpoint="reset-password",
    )


def normalize_auth_email(email: str) -> str:
    return _normalize_email(email)
