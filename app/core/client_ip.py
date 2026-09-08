"""Derive the client IP from a request behind trusted reverse proxies.

Never use the leftmost ``X-Forwarded-For`` hop blindly: clients can prepend
forged addresses. With ``trusted_proxy_count=N``, the client hop is at index
``len(hops) - 1 - N`` (count from the right).

Examples (N=1):

- No XFF → TCP peer (``request.client.host``), or ``default``.
- ``X-Forwarded-For: 203.0.113.10, 198.51.100.1``
  (client, immediate proxy) → ``203.0.113.10`` (left of the last hop).
- ``X-Forwarded-For: 203.0.113.10, 198.51.100.1, 192.0.2.60``
  → ``198.51.100.1`` — not the forged leftmost ``203.0.113.10``.

Invalid hop literals fall back to the TCP peer.
"""

from __future__ import annotations

import ipaddress
import hashlib
import hmac
import time

from starlette.requests import Request

from app.core.config import get_settings


def _peer_host(request: Request, default: str) -> str:
    if request.client and request.client.host:
        return request.client.host
    return default


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def get_client_ip(
    request: Request,
    *,
    trusted_proxy_count: int | None = None,
    default: str = "unknown",
) -> str:
    """Return the client IP using right-based trusted proxy hops.

    Args:
        request: Incoming ASGI/Starlette request.
        trusted_proxy_count: Override for ``settings.trusted_proxy_count``.
            Number of reverse-proxy hops to skip from the right of XFF.
        default: Value when there is no peer and no usable XFF hop.
    """
    peer = _peer_host(request, default)
    secret = get_settings().korus_proxy_secret
    signed_ip = request.headers.get("x-korus-client-ip", "")
    timestamp = request.headers.get("x-korus-client-time", "")
    signature = request.headers.get("x-korus-client-signature", "")
    if len(secret) >= 32 and _is_valid_ip(signed_ip):
        try:
            fresh = abs(time.time() - int(timestamp)) <= 60
        except ValueError:
            fresh = False
        expected = hmac.new(secret.encode(), f"{timestamp}\n{signed_ip}".encode(), hashlib.sha256).hexdigest()
        if fresh and len(signature) == 64 and signature.isascii() and hmac.compare_digest(signature, expected):
            return signed_ip
    n = (
        get_settings().trusted_proxy_count
        if trusted_proxy_count is None
        else trusted_proxy_count
    )
    if n < 0:
        n = 0

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded or not forwarded.strip():
        return peer

    hops = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not hops:
        return peer

    # Client sits N hops left of the rightmost entry (immediate proxy).
    idx = len(hops) - 1 - n
    if idx < 0:
        # Not enough hops to skip N trusted proxies — use TCP peer.
        return peer

    candidate = hops[idx]
    if not _is_valid_ip(candidate):
        return peer
    return candidate
