"""Evolution API WhatsApp integration: instance lifecycle and free-text sending."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import REMINDER_TEMPLATE_BODY
from app.core.config import get_settings
from app.models.notification_message_log import NotificationMessageLog
from app.models.whatsapp_connection import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_NEEDS_RECONNECT,
    CONNECTION_STATUS_SETUP_INCOMPLETE,
    WhatsAppConnection,
)
from app.services.evolution_api_client import EvolutionApiClient, EvolutionApiError
from app.services.evolution_webhook_auth import (
    map_evolution_message_status,
    normalize_evolution_event,
)
from app.services.whatsapp_types import WhatsAppSendResult
from app.utils.credential_encryption import (
    CredentialEncryptionError,
    decrypt_secret,
    encrypt_secret,
)

logger = logging.getLogger(__name__)


class EvolutionDeliveryUnknownError(RuntimeError):
    """Evolution may have accepted the message but no response was confirmed."""


_EVOLUTION_STATE_TO_CONNECTION = {
    "open": CONNECTION_STATUS_ACTIVE,
    "connecting": CONNECTION_STATUS_CONNECTING,
    "close": CONNECTION_STATUS_NEEDS_RECONNECT,
    "closed": CONNECTION_STATUS_NEEDS_RECONNECT,
}


def evolution_instance_name_for_professional(professional_id: UUID) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "", str(professional_id))[:24]
    return f"korus-{safe or 'default'}"


def normalize_whatsapp_number(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "").lstrip("0")
    if not digits:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Número de telefone inválido para envio WhatsApp.",
        )

    if digits.startswith("55"):
        normalized = digits
    elif len(digits) in (10, 11):
        normalized = f"55{digits}"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Telefone incompleto para WhatsApp. Cadastre DDD + número "
                f"(ex.: 11999990000). Valor recebido: {phone!r}."
            ),
        )

    if len(normalized) not in (12, 13):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Telefone deve ter 10 ou 11 dígitos com DDD para envio WhatsApp "
                f"(valor: {phone!r})."
            ),
        )

    ddd = int(normalized[2:4])
    if ddd < 11 or ddd > 99:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"DDD inválido no telefone cadastrado ({phone!r}).",
        )

    local_number = normalized[4:]
    if len(local_number) == 9 and not local_number.startswith("9"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Celular inválido: números móveis no Brasil começam com 9 "
                f"após o DDD ({phone!r})."
            ),
        )

    return normalized


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) <= 4:
        return digits
    return f"{digits[:4]}***{digits[-2:]}"


def whatsapp_number_candidates(phone: str) -> list[str]:
    primary = normalize_whatsapp_number(phone)
    candidates = [primary]
    if len(primary) == 12:
        local = primary[4:]
        if len(local) == 8 and local[0] in "6789":
            candidates.append(f"{primary[:4]}9{local}")
    return list(dict.fromkeys(candidates))


def _is_uuid_like(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
            re.I,
        )
    )


def format_reminder_text(variables: list[str]) -> str:
    text = REMINDER_TEMPLATE_BODY
    for idx, value in enumerate(variables[:5], start=1):
        text = text.replace(f"{{{{{idx}}}}}", str(value))
    return text


@dataclass
class EvolutionConnectResult:
    connection: WhatsAppConnection
    qrcode_base64: str | None = None
    connection_state: str | None = None


class EvolutionWhatsAppService:
    def __init__(self, db: AsyncSession, client: EvolutionApiClient | None = None):
        self.db = db
        self.client = client or EvolutionApiClient()

    async def get_active_connection(self, professional_id: UUID) -> WhatsAppConnection | None:
        result = await self.db.execute(
            select(WhatsAppConnection)
            .where(
                WhatsAppConnection.professional_id == professional_id,
                WhatsAppConnection.provider == "evolution",
                WhatsAppConnection.status != CONNECTION_STATUS_DISCONNECTED,
            )
            .order_by(WhatsAppConnection.created_at.desc())
        )
        return result.scalars().first()

    async def _latest_connection_for_instance(
        self, professional_id: UUID, instance_name: str
    ) -> WhatsAppConnection | None:
        result = await self.db.execute(
            select(WhatsAppConnection)
            .where(
                WhatsAppConnection.professional_id == professional_id,
                WhatsAppConnection.provider == "evolution",
                WhatsAppConnection.evolution_instance_name == instance_name,
            )
            .order_by(WhatsAppConnection.created_at.desc())
        )
        return result.scalars().first()

    async def can_send(self, professional_id: UUID) -> bool:
        settings = get_settings()
        if settings.whatsapp_provider != "evolution":
            return False
        connection = await self.get_active_connection(professional_id)
        return bool(connection and connection.status == CONNECTION_STATUS_ACTIVE)

    def _instance_api_key(self, connection: WhatsAppConnection) -> str:
        token = connection.encrypted_instance_api_key or connection.encrypted_access_token
        if not token:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Instância Evolution sem credencial configurada.",
            )
        try:
            return decrypt_secret(token)
        except CredentialEncryptionError as exc:
            # Key rotated / mismatch: Evolution accepts the global admin key for
            # instance ops. Prefer reconnect so the row is re-encrypted.
            settings = get_settings()
            if settings.evolution_global_api_key:
                logger.warning(
                    "Could not decrypt Evolution credential for connection %s; "
                    "falling back to EVOLUTION_GLOBAL_API_KEY. Reconnect WhatsApp "
                    "to store a fresh encrypted instance key.",
                    connection.id,
                )
                return settings.evolution_global_api_key
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Credencial WhatsApp não pôde ser lida (chave de criptografia "
                    "alterada). Desconecte e reconecte o WhatsApp."
                ),
            ) from exc

    def _instance_name(self, connection: WhatsAppConnection) -> str:
        name = connection.evolution_instance_name
        if not name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Instância Evolution sem nome configurado.",
            )
        return name

    async def _remote_cleanup(self, connection: WhatsAppConnection) -> None:
        if not connection.evolution_instance_name:
            return
        try:
            api_key = self._instance_api_key(connection)
        except HTTPException:
            api_key = get_settings().evolution_global_api_key
            if not api_key:
                return
        name = connection.evolution_instance_name
        try:
            await self.client.logout_instance(name, api_key=api_key)
        except EvolutionApiError as exc:
            logger.info("Evolution logout failed for %s: %s", name, exc.message)
        try:
            await self.client.delete_instance(name, api_key=api_key)
        except EvolutionApiError as exc:
            logger.info("Evolution delete failed for %s: %s", name, exc.message)

    async def _soft_disconnect_existing(self, professional_id: UUID) -> None:
        result = await self.db.execute(
            select(WhatsAppConnection).where(
                WhatsAppConnection.professional_id == professional_id,
                WhatsAppConnection.provider == "evolution",
                WhatsAppConnection.status != CONNECTION_STATUS_DISCONNECTED,
            )
        )
        for connection in result.scalars().all():
            await self._remote_cleanup(connection)
            connection.status = CONNECTION_STATUS_DISCONNECTED
            connection.disconnected_at = datetime.now(UTC)
        await self.db.flush()

    async def _ensure_webhook(self, instance_name: str, api_key: str) -> None:
        settings = get_settings()
        webhook_url = settings.evolution_webhook_url
        if not webhook_url:
            message = "APP_PUBLIC_URL not set; Evolution webhook cannot be registered."
            if settings.debug:
                logger.warning(message)
                return
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="APP_PUBLIC_URL é obrigatório para registrar o webhook Evolution.",
            )
        try:
            await self.client.set_webhook(
                instance_name,
                webhook_url,
                api_key=api_key,
                secret=settings.evolution_webhook_secret,
            )
        except EvolutionApiError as exc:
            if settings.debug:
                logger.warning("Failed to set Evolution webhook: %s", exc.message)
                return
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao registrar webhook Evolution: {exc.message}",
            ) from exc

    async def _sync_phone_from_instances(self, connection: WhatsAppConnection, api_key: str) -> None:
        try:
            listing = await self.client.fetch_instances(
                self._instance_name(connection), api_key=api_key
            )
        except EvolutionApiError:
            return
        rows = listing if isinstance(listing, list) else listing.get("data", [])
        if not rows and isinstance(listing, dict):
            rows = [listing]
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            number = row.get("number") or row.get("ownerJid")
            if number:
                connection.display_phone_number = str(number).split("@")[0]
            profile = row.get("profileName") or row.get("verified_name")
            if profile:
                connection.verified_name = str(profile)
            break

    def _apply_evolution_state(
        self, connection: WhatsAppConnection, evolution_state: str | None
    ) -> str:
        mapped = _EVOLUTION_STATE_TO_CONNECTION.get(
            (evolution_state or "").lower(), CONNECTION_STATUS_SETUP_INCOMPLETE
        )
        connection.status = mapped
        if mapped == CONNECTION_STATUS_ACTIVE:
            connection.connected_at = connection.connected_at or datetime.now(UTC)
        return mapped

    async def _resolve_api_key_for_new_instance(
        self, professional_id: UUID, instance_name: str, created: dict[str, Any]
    ) -> str:
        extracted = EvolutionApiClient.extract_instance_api_key(created)
        if extracted:
            return extracted

        previous = await self._latest_connection_for_instance(professional_id, instance_name)
        if previous and (previous.encrypted_instance_api_key or previous.encrypted_access_token):
            try:
                return self._instance_api_key(previous)
            except Exception:
                logger.warning(
                    "Could not reuse previous Evolution instance token for %s", instance_name
                )

        settings = get_settings()
        if settings.evolution_global_api_key:
            # Last resort only for admin-key Evolution installs that don't return per-instance hash.
            logger.warning(
                "Using EVOLUTION_GLOBAL_API_KEY as instance credential for %s; "
                "prefer deleting/recreating so Evolution returns a per-instance hash.",
                instance_name,
            )
            return settings.evolution_global_api_key

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Evolution não retornou apikey da instância.",
        )

    async def connect(self, professional_id: UUID) -> EvolutionConnectResult:
        instance_name = evolution_instance_name_for_professional(professional_id)
        existing = await self.get_active_connection(professional_id)

        if existing and existing.evolution_instance_name:
            try:
                api_key = self._instance_api_key(existing)
            except HTTPException:
                # Undecryptable row without global key — recreate below.
                existing = None
            else:
                instance_name = existing.evolution_instance_name
                try:
                    state_payload = await self.client.connection_state(
                        instance_name, api_key=api_key
                    )
                    evolution_state = EvolutionApiClient.extract_connection_state(
                        state_payload
                    )
                    self._apply_evolution_state(existing, evolution_state)
                    qrcode_base64 = None
                    if evolution_state in (None, "close", "closed", "connecting"):
                        connect_payload = await self.client.connect_instance(
                            instance_name, api_key=api_key
                        )
                        qrcode_base64 = EvolutionApiClient.extract_qrcode_base64(
                            connect_payload
                        )
                    await self._ensure_webhook(instance_name, api_key)
                    await self._sync_phone_from_instances(existing, api_key)
                    await self.db.commit()
                    await self.db.refresh(existing)
                    return EvolutionConnectResult(
                        connection=existing,
                        qrcode_base64=qrcode_base64,
                        connection_state=evolution_state,
                    )
                except HTTPException:
                    raise
                except EvolutionApiError as exc:
                    existing.last_error = exc.message
                    await self.db.commit()
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Falha ao conectar instância Evolution: {exc.message}",
                    ) from exc

        await self._soft_disconnect_existing(professional_id)

        settings = get_settings()
        webhook_url = settings.evolution_webhook_url
        try:
            created = await self.client.create_instance(
                instance_name,
                qrcode=True,
                webhook_url=webhook_url,
                webhook_secret=settings.evolution_webhook_secret or None,
            )
        except EvolutionApiError as exc:
            if exc.status_code == 403 or "already" in exc.message.lower():
                # Reuse stored token; if missing, delete remote orphan and recreate.
                previous = await self._latest_connection_for_instance(
                    professional_id, instance_name
                )
                reused_key = None
                if previous and (
                    previous.encrypted_instance_api_key or previous.encrypted_access_token
                ):
                    try:
                        reused_key = self._instance_api_key(previous)
                    except Exception:
                        reused_key = None
                if reused_key:
                    created = {}
                    # Stash for resolve below via previous row
                else:
                    global_key = settings.evolution_global_api_key
                    if global_key:
                        try:
                            await self.client.delete_instance(
                                instance_name, api_key=global_key
                            )
                        except EvolutionApiError:
                            pass
                        created = await self.client.create_instance(
                            instance_name,
                            qrcode=True,
                            webhook_url=webhook_url,
                            webhook_secret=settings.evolution_webhook_secret or None,
                        )
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=(
                                "Instância Evolution já existe e não há credencial "
                                "salva para reutilizá-la. Configure EVOLUTION_GLOBAL_API_KEY "
                                "ou limpe a instância no painel Evolution."
                            ),
                        ) from exc
            else:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Falha ao criar instância Evolution: {exc.message}",
                ) from exc

        api_key = await self._resolve_api_key_for_new_instance(
            professional_id, instance_name, created
        )

        connection = WhatsAppConnection(
            id=uuid.uuid4(),
            professional_id=professional_id,
            provider="evolution",
            status=CONNECTION_STATUS_CONNECTING,
            evolution_instance_name=instance_name,
            encrypted_instance_api_key=encrypt_secret(api_key),
        )
        self.db.add(connection)
        await self.db.flush()

        await self._ensure_webhook(instance_name, api_key)

        qrcode_base64 = EvolutionApiClient.extract_qrcode_base64(created)
        evolution_state = "connecting"
        try:
            if not qrcode_base64:
                connect_payload = await self.client.connect_instance(
                    instance_name, api_key=api_key
                )
                qrcode_base64 = EvolutionApiClient.extract_qrcode_base64(connect_payload)
            state_payload = await self.client.connection_state(instance_name, api_key=api_key)
            evolution_state = (
                EvolutionApiClient.extract_connection_state(state_payload) or evolution_state
            )
            self._apply_evolution_state(connection, evolution_state)
        except EvolutionApiError as exc:
            connection.last_error = exc.message
            connection.status = CONNECTION_STATUS_SETUP_INCOMPLETE

        await self.db.commit()
        await self.db.refresh(connection)
        return EvolutionConnectResult(
            connection=connection,
            qrcode_base64=qrcode_base64,
            connection_state=evolution_state,
        )

    async def refresh_connection(self, professional_id: UUID) -> WhatsAppConnection:
        connection = await self.get_active_connection(professional_id)
        if not connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Nenhuma conexão WhatsApp Evolution encontrada.",
            )
        api_key = self._instance_api_key(connection)
        instance_name = self._instance_name(connection)
        try:
            state_payload = await self.client.connection_state(instance_name, api_key=api_key)
            evolution_state = EvolutionApiClient.extract_connection_state(state_payload)
            self._apply_evolution_state(connection, evolution_state)
            if evolution_state == "connecting":
                connect_payload = await self.client.connect_instance(
                    instance_name, api_key=api_key
                )
                _ = EvolutionApiClient.extract_qrcode_base64(connect_payload)
            await self._ensure_webhook(instance_name, api_key)
            await self._sync_phone_from_instances(connection, api_key)
        except HTTPException:
            raise
        except EvolutionApiError as exc:
            connection.last_error = exc.message
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao atualizar status Evolution: {exc.message}",
            ) from exc
        await self.db.commit()
        await self.db.refresh(connection)
        return connection

    async def disconnect(self, professional_id: UUID) -> WhatsAppConnection | None:
        connection = await self.get_active_connection(professional_id)
        if not connection:
            return None
        await self._remote_cleanup(connection)
        connection.status = CONNECTION_STATUS_DISCONNECTED
        connection.disconnected_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(connection)
        return connection

    async def get_usage(self, professional_id: UUID) -> dict[str, Any]:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        result = await self.db.execute(
            select(func.count(NotificationMessageLog.id)).where(
                and_(
                    NotificationMessageLog.professional_id == professional_id,
                    NotificationMessageLog.channel == "whatsapp",
                    NotificationMessageLog.is_test.is_(False),
                    NotificationMessageLog.status.in_(("sent", "delivered", "read")),
                    NotificationMessageLog.created_at >= month_start,
                )
            )
        )
        used = int(result.scalar_one() or 0)
        return {
            "month": now.strftime("%Y-%m"),
            "used": used,
            "limit": 0,
            "remaining": 0,
        }

    async def _resolve_instance_name(self, connection: WhatsAppConnection, api_key: str) -> str:
        stored = connection.evolution_instance_name
        if not stored:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Instância Evolution sem nome configurado.",
            )

        try:
            listing = await self.client.fetch_instances(
                None if _is_uuid_like(stored) else stored,
                api_key=api_key,
            )
        except EvolutionApiError:
            return stored

        rows = listing if isinstance(listing, list) else []
        if isinstance(listing, dict):
            data = listing.get("data")
            rows = data if isinstance(data, list) else [listing]

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("instanceName")
            instance_id = str(row.get("id") or "")
            if not name:
                continue
            if stored == name or (instance_id and stored == instance_id):
                if connection.evolution_instance_name != name:
                    connection.evolution_instance_name = name
                    await self.db.commit()
                return str(name)

        if _is_uuid_like(stored):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Instância Evolution inconsistente. Reconecte o WhatsApp "
                    "na aba WhatsApp (o sistema estava usando o ID em vez do nome)."
                ),
            )
        return stored

    async def _resolve_send_number(
        self,
        instance_name: str,
        recipient_phone: str,
        api_key: str,
    ) -> str:
        candidates = whatsapp_number_candidates(recipient_phone)
        try:
            checks = await self.client.check_whatsapp_numbers(
                instance_name, candidates, api_key=api_key
            )
        except EvolutionApiError as exc:
            logger.warning(
                "Evolution whatsappNumbers check failed for %s: %s",
                mask_phone(candidates[0]),
                exc.message,
            )
            return candidates[0]

        for candidate in candidates:
            for row in checks:
                row_number = re.sub(r"\D", "", str(row.get("number") or ""))
                if row_number != candidate:
                    continue
                if row.get("exists"):
                    jid = row.get("jid")
                    if isinstance(jid, str) and jid:
                        return jid.split("@")[0] if "@" in jid else jid
                    return candidate

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"O telefone {mask_phone(candidates[0])} não possui WhatsApp ativo "
                "ou está incorreto. Atualize o cadastro do responsável."
            ),
        )

    async def _ensure_connection_open(self, connection: WhatsAppConnection) -> None:
        api_key = self._instance_api_key(connection)
        instance_name = await self._resolve_instance_name(connection, api_key)
        try:
            state_payload = await self.client.connection_state(instance_name, api_key=api_key)
        except EvolutionApiError as exc:
            connection.status = CONNECTION_STATUS_NEEDS_RECONNECT
            connection.last_error = exc.message
            await self.db.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Não foi possível verificar o status do WhatsApp Evolution. "
                    f"Tente novamente em instantes ({exc.message})."
                ),
            ) from exc

        evolution_state = EvolutionApiClient.extract_connection_state(state_payload)
        mapped = self._apply_evolution_state(connection, evolution_state)
        await self.db.commit()
        if mapped != CONNECTION_STATUS_ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "WhatsApp Evolution desconectado. Abra a aba WhatsApp e "
                    f"reconecte o número (status: {evolution_state or 'desconhecido'})."
                ),
            )

    async def _send_text(
        self,
        connection: WhatsAppConnection,
        recipient_phone: str,
        text: str,
    ) -> WhatsAppSendResult:
        api_key = self._instance_api_key(connection)
        await self._ensure_connection_open(connection)
        instance_name = await self._resolve_instance_name(connection, api_key)
        number = await self._resolve_send_number(instance_name, recipient_phone, api_key)

        try:
            response = await self.client.send_text(instance_name, number, text, api_key=api_key)
        except EvolutionApiError as exc:
            if "close" in exc.message.lower() or exc.status_code == 401:
                connection.status = CONNECTION_STATUS_NEEDS_RECONNECT
                connection.last_error = exc.message
                await self.db.commit()
            if exc.status_code is None or exc.status_code >= 500:
                raise EvolutionDeliveryUnknownError(
                    f"Resultado do envio Evolution desconhecido: {exc.message}"
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao enviar mensagem Evolution: {exc.message}",
            ) from exc

        message_id = None
        key = response.get("key") if isinstance(response, dict) else None
        if isinstance(key, dict):
            message_id = key.get("id")
        if not message_id and isinstance(response, dict):
            message_id = response.get("messageId") or response.get("id")
        return WhatsAppSendResult(
            provider="evolution",
            provider_message_id=str(message_id) if message_id else None,
            status="sent",
            payload=response if isinstance(response, dict) else {},
        )

    async def send_text_message(
        self,
        professional_id: UUID,
        recipient_phone: str,
        text: str,
    ) -> WhatsAppSendResult:
        connection = await self.get_active_connection(professional_id)
        if not connection or connection.status != CONNECTION_STATUS_ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conexão WhatsApp Evolution não está ativa.",
            )
        return await self._send_text(connection, recipient_phone, text)

    async def send_appointment_reminder(
        self,
        professional_id: UUID,
        recipient_phone: str,
        variables: list[str],
    ) -> WhatsAppSendResult:
        return await self.send_text_message(
            professional_id, recipient_phone, format_reminder_text(variables)
        )

    async def handle_webhook_event(self, payload: dict[str, Any]) -> None:
        event = normalize_evolution_event(str(payload.get("event") or ""))
        instance_name = payload.get("instance")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        if not instance_name:
            return

        result = await self.db.execute(
            select(WhatsAppConnection).where(
                WhatsAppConnection.provider == "evolution",
                WhatsAppConnection.evolution_instance_name == str(instance_name),
                WhatsAppConnection.status != CONNECTION_STATUS_DISCONNECTED,
            )
        )
        connection = result.scalars().first()
        if not connection:
            return

        if event in ("connection.update", "qrcode.updated"):
            state = None
            if isinstance(data, dict):
                state = data.get("state") or data.get("status")
            state = state or payload.get("state")
            if state:
                self._apply_evolution_state(connection, str(state))
                if isinstance(data, dict):
                    wuid = data.get("wuid") or data.get("ownerJid")
                    if wuid:
                        connection.display_phone_number = str(wuid).split("@")[0]
                    if data.get("profileName"):
                        connection.verified_name = str(data["profileName"])

        if event in ("send.message", "messages.update"):
            message_id = None
            status_value = None
            if isinstance(data, dict):
                key = data.get("key") if isinstance(data.get("key"), dict) else {}
                message_id = key.get("id") or data.get("messageId")
                status_value = data.get("status")
                if status_value is None and isinstance(data.get("update"), dict):
                    status_value = data["update"].get("status")
            mapped_status = map_evolution_message_status(status_value)
            if message_id and mapped_status:
                log_result = await self.db.execute(
                    select(NotificationMessageLog).where(
                        NotificationMessageLog.provider == "evolution",
                        NotificationMessageLog.provider_message_id == str(message_id),
                    )
                )
                log = log_result.scalars().first()
                if log:
                    log.status = mapped_status
                    persisted_payload = dict(log.payload or {})
                    persisted_payload["provider_webhook"] = (
                        data if isinstance(data, dict) else payload
                    )
                    log.payload = persisted_payload
                    now = datetime.now(UTC)
                    if mapped_status in ("delivered", "read") and not log.delivered_at:
                        log.delivered_at = now
                    if mapped_status == "read" and not log.read_at:
                        log.read_at = now
                    if mapped_status == "failed" and not log.failed_at:
                        log.failed_at = now

        await self.db.commit()
