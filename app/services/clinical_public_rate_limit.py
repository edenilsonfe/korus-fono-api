"""Rate limiting for public clinical mutations (F20 onward).

Reusable surface for no-auth clinical flows: the caller passes a namespace, an
already-hashed identifier and the numeric parameters. Raw tokens never reach
this module, the Redis keys or the logs. Unlike the auth limiter
(``auth_rate_limit``, which keeps its own policy), public mutations are
fail-closed: when the counter store is unreachable the request is refused with
503 instead of being silently allowed.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import HTTPException, status
from starlette.requests import Request

from app.core.client_ip import get_client_ip
from app.core.config import get_settings

logger = logging.getLogger(__name__)

UNAVAILABLE_DETAIL = (
    "Serviço temporariamente indisponível. Tente novamente em instantes."
)

# F20 — public acknowledgement of a school report delivery.
REPORT_DELIVERY_ACK_NAMESPACE = "clinical:report-delivery-ack"
REPORT_DELIVERY_ACK_TOKEN_LIMIT = 10  # per token
REPORT_DELIVERY_ACK_IP_LIMIT = 60  # per trusted client IP
REPORT_DELIVERY_ACK_WINDOW_SECONDS = 60
REPORT_DELIVERY_ACK_DETAIL = (
    "Muitas confirmações de recebimento. Tente novamente em instantes."
)

# F16 — public family responses of a home program (header X-Home-Program-Token).
HOME_PROGRAM_RESPONSE_NAMESPACE = "clinical:home-program-response"
HOME_PROGRAM_RESPONSE_READ_LIMIT = 60  # per grant
HOME_PROGRAM_RESPONSE_WRITE_LIMIT = 30  # per grant
HOME_PROGRAM_RESPONSE_IP_LIMIT = 120  # per trusted client IP
HOME_PROGRAM_RESPONSE_WINDOW_SECONDS = 60
HOME_PROGRAM_RESPONSE_UPLOAD_LIMIT = 10  # per grant
HOME_PROGRAM_RESPONSE_UPLOAD_WINDOW_SECONDS = 600  # 10 minutos
HOME_PROGRAM_RESPONSE_READ_DETAIL = (
    "Muitas consultas ao programa de casa. Tente novamente em instantes."
)
HOME_PROGRAM_RESPONSE_WRITE_DETAIL = (
    "Muitas respostas ao programa de casa. Tente novamente em instantes."
)
HOME_PROGRAM_RESPONSE_UPLOAD_DETAIL = (
    "Muitos envios de foto ao programa de casa. Tente novamente em instantes."
)
HOME_PROGRAM_RESPONSE_IP_DETAIL = (
    "Muitas solicitações ao programa de casa. Tente novamente em instantes."
)


def hash_identifier(value: str) -> str:
    """SHA-256 hex of an opaque identifier — the only form used as a key part."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _redis_allow(*, key: str, max_requests: int, window_seconds: int) -> bool:
    import redis

    settings = get_settings()
    client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        current, _ = pipe.execute()
        return int(current) <= max_requests
    finally:
        client.close()


def enforce_public_rate_limit(
    *,
    namespace: str,
    identifier_hash: str,
    max_requests: int,
    window_seconds: int,
    detail: str,
    endpoint: str,
    retry_after_seconds: int | None = None,
) -> None:
    """Consume one counter for ``namespace`` + hashed identifier.

    Raises 429 with ``Retry-After`` when the budget is exhausted and 503
    (fail-closed) when the counter store is unreachable. ``identifier_hash``
    must already be a digest: raw tokens/IPs are never used as Redis keys.
    """
    key = f"{namespace}:{identifier_hash}"
    try:
        allowed = _redis_allow(
            key=key, max_requests=max_requests, window_seconds=window_seconds
        )
    except Exception as exc:  # noqa: BLE001 - public mutations fail closed
        logger.error("%s rate limit unavailable (fail-closed): %s", endpoint, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=UNAVAILABLE_DETAIL,
        ) from exc
    if not allowed:
        retry_after = (
            window_seconds if retry_after_seconds is None else retry_after_seconds
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": str(retry_after)},
        )


def enforce_report_delivery_acknowledgement_rate_limit(
    request: Request, *, token_hash: str
) -> None:
    """F20: 10/min per delivery token and 60/min per trusted client IP."""
    enforce_public_rate_limit(
        namespace=f"{REPORT_DELIVERY_ACK_NAMESPACE}:token",
        identifier_hash=token_hash,
        max_requests=REPORT_DELIVERY_ACK_TOKEN_LIMIT,
        window_seconds=REPORT_DELIVERY_ACK_WINDOW_SECONDS,
        detail=REPORT_DELIVERY_ACK_DETAIL,
        endpoint="report-delivery-acknowledgement",
    )
    enforce_public_rate_limit(
        namespace=f"{REPORT_DELIVERY_ACK_NAMESPACE}:ip",
        identifier_hash=hash_identifier(get_client_ip(request)),
        max_requests=REPORT_DELIVERY_ACK_IP_LIMIT,
        window_seconds=REPORT_DELIVERY_ACK_WINDOW_SECONDS,
        detail=REPORT_DELIVERY_ACK_DETAIL,
        endpoint="report-delivery-acknowledgement",
    )


def enforce_home_program_response_ip_rate_limit(request: Request) -> None:
    """F16: 120 requests/min per trusted client IP (reads and writes)."""
    enforce_public_rate_limit(
        namespace=f"{HOME_PROGRAM_RESPONSE_NAMESPACE}:ip",
        identifier_hash=hash_identifier(get_client_ip(request)),
        max_requests=HOME_PROGRAM_RESPONSE_IP_LIMIT,
        window_seconds=HOME_PROGRAM_RESPONSE_WINDOW_SECONDS,
        detail=HOME_PROGRAM_RESPONSE_IP_DETAIL,
        endpoint="home-program-response",
    )


def enforce_home_program_response_read_rate_limit(*, grant_hash: str) -> None:
    """F16: 60 reads/min per grant (hashed grant id; never the raw token)."""
    enforce_public_rate_limit(
        namespace=f"{HOME_PROGRAM_RESPONSE_NAMESPACE}:read",
        identifier_hash=grant_hash,
        max_requests=HOME_PROGRAM_RESPONSE_READ_LIMIT,
        window_seconds=HOME_PROGRAM_RESPONSE_WINDOW_SECONDS,
        detail=HOME_PROGRAM_RESPONSE_READ_DETAIL,
        endpoint="home-program-response",
    )


def enforce_home_program_response_write_rate_limit(*, grant_hash: str) -> None:
    """F16: 30 check-in commands/min per grant (fail-closed 503 without store)."""
    enforce_public_rate_limit(
        namespace=f"{HOME_PROGRAM_RESPONSE_NAMESPACE}:write",
        identifier_hash=grant_hash,
        max_requests=HOME_PROGRAM_RESPONSE_WRITE_LIMIT,
        window_seconds=HOME_PROGRAM_RESPONSE_WINDOW_SECONDS,
        detail=HOME_PROGRAM_RESPONSE_WRITE_DETAIL,
        endpoint="home-program-response",
    )


def enforce_home_program_response_upload_rate_limit(*, grant_hash: str) -> None:
    """F16: 10 foto uploads/10 min per grant (foto é I/O caro; teto próprio)."""
    enforce_public_rate_limit(
        namespace=f"{HOME_PROGRAM_RESPONSE_NAMESPACE}:upload",
        identifier_hash=grant_hash,
        max_requests=HOME_PROGRAM_RESPONSE_UPLOAD_LIMIT,
        window_seconds=HOME_PROGRAM_RESPONSE_UPLOAD_WINDOW_SECONDS,
        detail=HOME_PROGRAM_RESPONSE_UPLOAD_DETAIL,
        endpoint="home-program-response",
        retry_after_seconds=HOME_PROGRAM_RESPONSE_UPLOAD_WINDOW_SECONDS,
    )
