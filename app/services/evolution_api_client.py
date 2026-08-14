"""Thin async client for Evolution API v2.3 (instances, messages, webhooks)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_WEBHOOK_EVENTS = [
    "QRCODE_UPDATED",
    "CONNECTION_UPDATE",
    "SEND_MESSAGE",
    "MESSAGES_UPDATE",
]


class EvolutionApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class EvolutionApiClient:
    _shared_client: httpx.AsyncClient | None = None

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout

    @classmethod
    async def aclose_shared(cls) -> None:
        client = cls._shared_client
        cls._shared_client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def _http(self) -> httpx.AsyncClient:
        client = EvolutionApiClient._shared_client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=self._timeout)
            EvolutionApiClient._shared_client = client
        return client

    @property
    def base_url(self) -> str:
        settings = get_settings()
        url = (settings.evolution_api_base_url or "").strip().rstrip("/")
        if not url:
            raise EvolutionApiError("EVOLUTION_API_BASE_URL is not configured.")
        return url

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        settings = get_settings()
        key = api_key or settings.evolution_global_api_key
        if not key:
            raise EvolutionApiError(
                "Evolution API key is not configured (global or instance token)."
            )
        return {"apikey": key, "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            client = await self._http()
            response = await client.request(
                method,
                url,
                json=json_body,
                headers=self._headers(api_key),
            )
        except httpx.RequestError as exc:
            raise EvolutionApiError(
                f"Falha ao contactar Evolution API ({url}): {exc}"
            ) from exc
        return self._parse(response)

    @staticmethod
    def _extract_error_message(payload: Any, response: httpx.Response) -> str:
        if not isinstance(payload, dict):
            text = (response.text or "").strip()
            return text[:500] if text else f"Evolution API HTTP {response.status_code}"

        parts: list[str] = []
        for key in ("message", "error"):
            value = payload.get(key)
            if value and str(value) not in parts:
                parts.append(str(value))

        nested = payload.get("response")
        if isinstance(nested, dict):
            nested_message = nested.get("message")
            if nested_message is not None:
                parts.append(str(nested_message))
        elif isinstance(nested, str) and nested:
            parts.append(nested)

        if parts:
            return " — ".join(parts)
        text = (response.text or "").strip()
        return text[:500] if text else f"Evolution API HTTP {response.status_code}"

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            message = EvolutionApiClient._extract_error_message(payload, response)
            raise EvolutionApiError(message, status_code=response.status_code)
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    @staticmethod
    def webhook_config(webhook_url: str, *, secret: str | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return {
            "enabled": True,
            "url": webhook_url,
            "headers": headers,
            "byEvents": False,
            "base64": False,
            "events": list(_WEBHOOK_EVENTS),
        }

    async def create_instance(
        self,
        instance_name: str,
        *,
        qrcode: bool = True,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instanceName": instance_name,
            "qrcode": qrcode,
            "integration": "WHATSAPP-BAILEYS",
        }
        if webhook_url:
            body["webhook"] = self.webhook_config(webhook_url, secret=webhook_secret)
        return await self._request("POST", "/instance/create", json_body=body)

    async def connect_instance(self, instance_name: str, *, api_key: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/instance/connect/{instance_name}",
            api_key=api_key,
        )

    async def connection_state(self, instance_name: str, *, api_key: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/instance/connectionState/{instance_name}",
            api_key=api_key,
        )

    async def fetch_instances(
        self, instance_name: str | None = None, *, api_key: str | None = None
    ) -> Any:
        path = "/instance/fetchInstances"
        if instance_name:
            path = f"{path}?instanceName={instance_name}"
        return await self._request("GET", path, api_key=api_key)

    async def logout_instance(self, instance_name: str, *, api_key: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/instance/logout/{instance_name}",
            api_key=api_key,
        )

    async def delete_instance(self, instance_name: str, *, api_key: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/instance/delete/{instance_name}",
            api_key=api_key,
        )

    async def check_whatsapp_numbers(
        self,
        instance_name: str,
        numbers: list[str],
        *,
        api_key: str,
    ) -> list[dict[str, Any]]:
        cleaned = [n for n in numbers if n]
        if not cleaned:
            return []
        result = await self._request(
            "POST",
            f"/chat/whatsappNumbers/{instance_name}",
            json_body={"numbers": cleaned},
            api_key=api_key,
        )
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
        return []

    async def send_text(
        self,
        instance_name: str,
        number: str,
        text: str,
        *,
        api_key: str,
    ) -> dict[str, Any]:
        message = (text or "").strip()
        if not message:
            raise EvolutionApiError("Texto da mensagem vazio.")

        # Evolution v2.3 primary shape; legacy textMessage kept as one fallback.
        payload_variants: list[dict[str, Any]] = [
            {"number": number, "text": message, "linkPreview": False, "delay": 1000},
            {"number": number, "textMessage": {"text": message}, "delay": 1000},
        ]

        errors: list[str] = []
        for body in payload_variants:
            try:
                return await self._request(
                    "POST",
                    f"/message/sendText/{instance_name}",
                    json_body=body,
                    api_key=api_key,
                )
            except EvolutionApiError as exc:
                errors.append(exc.message)
                # Only a deterministic schema rejection proves that Evolution did
                # not accept the first message. Retrying after a timeout or 5xx can
                # create a second WhatsApp delivery for one local log row.
                if exc.status_code not in {400, 422}:
                    raise
                continue

        raise EvolutionApiError(
            errors[-1] if errors else "Falha ao enviar mensagem Evolution.",
            status_code=400,
        )

    async def set_webhook(
        self,
        instance_name: str,
        webhook_url: str,
        *,
        api_key: str,
        secret: str | None = None,
    ) -> dict[str, Any]:
        body = {"webhook": self.webhook_config(webhook_url, secret=secret)}
        return await self._request(
            "POST",
            f"/webhook/set/{instance_name}",
            json_body=body,
            api_key=api_key,
        )

    @staticmethod
    def extract_qrcode_base64(payload: dict[str, Any]) -> str | None:
        if not isinstance(payload, dict):
            return None
        qrcode = payload.get("qrcode")
        if isinstance(qrcode, dict):
            value = qrcode.get("base64")
            if value:
                return str(value)
        for key in ("base64", "code"):
            if payload.get(key):
                return str(payload[key])
        return None

    @staticmethod
    def extract_instance_api_key(payload: dict[str, Any]) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("hash", "apikey", "token"):
            if payload.get(key):
                return str(payload[key])
        instance = payload.get("instance")
        if isinstance(instance, dict) and instance.get("token"):
            return str(instance["token"])
        return None

    @staticmethod
    def extract_connection_state(payload: dict[str, Any]) -> str | None:
        if not isinstance(payload, dict):
            return None
        instance = payload.get("instance")
        if isinstance(instance, dict) and instance.get("state"):
            return str(instance["state"]).lower()
        if payload.get("state"):
            return str(payload["state"]).lower()
        if payload.get("connectionStatus"):
            return str(payload["connectionStatus"]).lower()
        return None
