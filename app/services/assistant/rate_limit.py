"""Rate limiting for the assistant endpoint (Redis with in-memory fallback)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import DefaultDict

from fastapi import HTTPException, status

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_in_memory_buckets: DefaultDict[str, list[float]] = defaultdict(list)
_memory_lock = Lock()


def _check_memory(key: str, max_requests: int, window_seconds: int) -> bool:
    now = time.time()
    window_start = now - window_seconds
    with _memory_lock:
        bucket = [ts for ts in _in_memory_buckets[key] if ts > window_start]
        if len(bucket) >= max_requests:
            _in_memory_buckets[key] = bucket
            return False
        bucket.append(now)
        _in_memory_buckets[key] = bucket
    return True


def _redis_check(key: str, max_requests: int, window_seconds: int) -> bool:
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


def check_assistant_rate_limit(professional_id: str) -> bool:
    """Return True if the request is allowed, False if the limit is exceeded.

    Uses Redis as a shared counter and transparently falls back to a per-process
    in-memory sliding window when Redis is unavailable (fail-open on unexpected
    errors beyond the limit check itself).
    """
    settings = get_settings()
    max_requests = settings.assistant_rate_limit_per_hour
    window_seconds = 3600
    key = f"assistant:rl:{professional_id}"

    try:
        return _redis_check(key, max_requests, window_seconds)
    except Exception as exc:
        logger.warning("Assistant rate limit Redis unavailable, using in-memory fallback: %s", exc)
        return _check_memory(key, max_requests, window_seconds)


def enforce_assistant_rate_limit(professional_id: str) -> None:
    """Raise 429 if the professional exceeded the configured rate limit."""
    if not check_assistant_rate_limit(professional_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de mensagens ao assistente atingido. Tente novamente em instantes.",
            headers={"Retry-After": "3600"},
        )
