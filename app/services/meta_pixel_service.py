"""Meta Pixel / Conversions API (server-side events).

Envia eventos de conversão (CompleteRegistration, StartTrial, InitiateCheckout,
Purchase, etc.) direto à Graph API da Meta usando o access token do servidor.
O snippet do navegador (korus-one-web) continua sendo a fonte de eventos
client-side; aqui fazemos CAPI com o mesmo `event_id` para deduplicação.

A integração é best-effort: nunca lança exceção para fora (só loga) e desliga
completo quando META_PIXEL_ID / META_CAPI_ACCESS_TOKEN estão vazios.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com"


def _hash(value: str | None) -> str | None:
    """Normaliza e hasheia PII no formato exigido pela Meta (SHA-256, lowercase)."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    dt = value
    if hasattr(value, "isoformat"):
        try:
            dt = value.isoformat()
        except (TypeError, ValueError):
            return None
    if not isinstance(dt, str):
        return None
    return dt


class MetaPixelService:
    """Cliente do Conversions API da Meta (server-side events)."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        s = self.settings
        return bool((s.meta_pixel_id or "").strip() and (s.meta_capi_access_token or "").strip())

    @property
    def pixel_id(self) -> str:
        return (self.settings.meta_pixel_id or "").strip()

    def build_user_data(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        fbp: str | None = None,
        fbc: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if email:
            data["em"] = [_hash(email)]
        if phone:
            data["ph"] = [_hash(phone)]
        if first_name:
            data["fn"] = [_hash(first_name)]
        if last_name:
            data["ln"] = [_hash(last_name)]
        if client_ip:
            data["client_ip_address"] = client_ip
        if client_user_agent:
            data["client_user_agent"] = client_user_agent
        if fbp:
            data["fbp"] = fbp
        if fbc:
            data["fbc"] = fbc
        return data

    async def send_event(
        self,
        *,
        event_name: str,
        event_id: str,
        user_data: dict[str, Any],
        custom_data: dict[str, Any] | None = None,
        event_source_url: str | None = None,
        event_time: datetime | None = None,
        action_source: str = "website",
    ) -> bool:
        """Envia um evento à Graph API. Retorna False se desligado ou se a Meta rejeitar."""
        if not self.enabled:
            return False
        s = self.settings
        url = f"{_GRAPH_BASE}/{s.meta_graph_api_version}/{s.meta_pixel_id}/events"
        event: dict[str, Any] = {
            "event_name": event_name,
            "event_time": int((event_time or datetime.now(UTC)).timestamp()),
            "event_id": event_id,
            "event_source_url": event_source_url,
            "action_source": action_source,
            "user_data": user_data,
        }
        if custom_data:
            event["custom_data"] = custom_data
        payload: dict[str, Any] = {
            "data": [event],
            "access_token": s.meta_capi_access_token,
        }
        if (s.meta_capi_test_event_code or "").strip():
            payload["test_event_code"] = s.meta_capi_test_event_code.strip()

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
            if body.get("error"):
                logger.warning("Meta CAPI rejeitou %s (%s): %s", event_name, event_id, body["error"])
                return False
            received = bool(body.get("events_received") == 1)
            if not received:
                logger.warning(
                    "Meta CAPI: %s (%s) não confirmado: %s", event_name, event_id, body
                )
            return received
        except httpx.HTTPError as exc:
            logger.warning("Meta CAPI falhou para %s (%s): %s", event_name, event_id, exc)
            return False

    async def track_registration(
        self,
        *,
        professional_id: str,
        email: str,
        name: str,
        phone: str | None = None,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        fbp: str | None = None,
        fbc: str | None = None,
    ) -> bool:
        """CompleteRegistration — registro de profissional concluído (dedup por id)."""
        parts = name.split(maxsplit=1)
        user_data = self.build_user_data(
            email=email,
            phone=phone,
            first_name=parts[0] if parts else name,
            last_name=parts[1] if len(parts) > 1 else None,
            client_ip=client_ip,
            client_user_agent=client_user_agent,
            fbp=fbp,
            fbc=fbc,
        )
        return await self.send_event(
            event_name="CompleteRegistration",
            event_id=f"register-{professional_id}",
            user_data=user_data,
            event_source_url=None,
        )

    async def track_start_trial(
        self,
        *,
        professional_id: str,
        email: str,
        name: str,
        phone: str | None = None,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        fbp: str | None = None,
        fbc: str | None = None,
    ) -> bool:
        """StartTrial — trial iniciado no registro (dedup por id)."""
        parts = name.split(maxsplit=1)
        user_data = self.build_user_data(
            email=email,
            phone=phone,
            first_name=parts[0] if parts else name,
            last_name=parts[1] if len(parts) > 1 else None,
            client_ip=client_ip,
            client_user_agent=client_user_agent,
            fbp=fbp,
            fbc=fbc,
        )
        return await self.send_event(
            event_name="StartTrial",
            event_id=f"trial-{professional_id}",
            user_data=user_data,
        )

    async def track_checkout_started(
        self,
        *,
        professional_id: str,
        email: str,
        name: str,
        value_cents: int,
        currency: str,
        plan_slug: str,
        client_ip: str | None = None,
        client_user_agent: str | None = None,
        fbp: str | None = None,
        fbc: str | None = None,
    ) -> bool:
        """InitiateCheckout — usuário iniciou o checkout de um plano."""
        user_data = self.build_user_data(
            email=email,
            phone=None,
            first_name=name,
            client_ip=client_ip,
            client_user_agent=client_user_agent,
            fbp=fbp,
            fbc=fbc,
        )
        return await self.send_event(
            event_name="InitiateCheckout",
            event_id=f"checkout-{professional_id}",
            user_data=user_data,
            custom_data={
                "value": round(value_cents / 100, 2),
                "currency": currency,
                "content_ids": [plan_slug],
                "content_type": "product",
                "contents": [{"id": plan_slug, "quantity": 1, "item_price": round(value_cents / 100, 2)}],
            },
        )

    async def track_purchase(
        self,
        *,
        professional_id: str,
        email: str,
        name: str,
        value_cents: int,
        currency: str,
        plan_slug: str,
        billing_event_id: str,
    ) -> bool:
        """Purchase — pagamento confirmado (webhook/reconciliação). Dedup pelo evento de billing."""
        user_data = self.build_user_data(email=email, phone=None, first_name=name)
        return await self.send_event(
            event_name="Purchase",
            event_id=f"purchase-{billing_event_id}",
            user_data=user_data,
            custom_data={
                "value": round(value_cents / 100, 2),
                "currency": currency,
                "content_ids": [plan_slug],
                "content_type": "product",
                "contents": [{"id": plan_slug, "quantity": 1, "item_price": round(value_cents / 100, 2)}],
            },
        )
