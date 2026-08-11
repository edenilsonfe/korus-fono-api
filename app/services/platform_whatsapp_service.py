"""Platform WhatsApp connection service (Evolution).

Mirrors the professional flow (`EvolutionWhatsAppService`) but for the
platform's own number, managed from the admin panel. Used to send system
messages such as the registration welcome message. The connection is a
singleton row in `platform_whatsapp_connections`.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.constants.whatsapp_events import DEFAULT_WELCOME_MESSAGE
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.whatsapp_connection import (
    CONNECTION_STATUS_ACTIVE,
    CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_NEEDS_RECONNECT,
    CONNECTION_STATUS_NOT_CONNECTED,
    CONNECTION_STATUS_SETUP_INCOMPLETE,
)
from app.services.evolution_api_client import EvolutionApiClient, EvolutionApiError
from app.services.evolution_webhook_auth import normalize_evolution_event
from app.services.evolution_whatsapp_service import (
    mask_phone,
    whatsapp_number_candidates,
)
from app.services.whatsapp_types import WhatsAppSendResult
from app.utils.credential_encryption import (
    CredentialEncryptionError,
    decrypt_secret,
    encrypt_secret,
)

logger = logging.getLogger(__name__)

_EVOLUTION_STATE_TO_CONNECTION = {
    "open": CONNECTION_STATUS_ACTIVE,
    "connecting": CONNECTION_STATUS_CONNECTING,
    "close": CONNECTION_STATUS_NEEDS_RECONNECT,
    "closed": CONNECTION_STATUS_NEEDS_RECONNECT,
}


@dataclass
class PlatformConnectResult:
    connection: PlatformWhatsAppConnection
    qrcode_base64: str | None = None
    connection_state: str | None = None


class PlatformWhatsAppService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.client = EvolutionApiClient()

    # ------------------------------------------------------------------ state

    async def get_connection(self) -> PlatformWhatsAppConnection:
        """Return the singleton connection row, creating it if missing."""
        result = await self.db.execute(
            select(PlatformWhatsAppConnection).limit(1)
        )
        connection = result.scalars().first()
        if connection is None:
            connection = PlatformWhatsAppConnection(id=uuid.uuid4())
            self.db.add(connection)
            await self.db.flush()
        return connection

    async def can_send(self, connection: PlatformWhatsAppConnection | None = None) -> bool:
        if get_settings().whatsapp_provider != "evolution":
            return False
        if connection is None:
            connection = await self.get_connection()
        return bool(
            connection.status == CONNECTION_STATUS_ACTIVE
            and connection.evolution_instance_name
        )

    async def reconcile_status(
        self, connection: PlatformWhatsAppConnection | None = None
    ) -> PlatformWhatsAppConnection:
        """Reconcile the persisted status with Evolution without raising.

        Webhooks remain the fast path, but a missed lifecycle event must not
        leave the admin panel or welcome sender trusting a stale ``active`` row.
        """
        if connection is None:
            connection = await self.get_connection()
        if (
            get_settings().whatsapp_provider != "evolution"
            or not connection.evolution_instance_name
        ):
            return connection

        try:
            api_key = self._api_key(connection)
            state_payload = await self.client.connection_state(
                connection.evolution_instance_name, api_key=api_key
            )
            evolution_state = self.client.extract_connection_state(state_payload)
            mapped = self._apply_evolution_state(connection, evolution_state)
            connection.last_error = (
                None
                if mapped == CONNECTION_STATUS_ACTIVE
                else f"Conexão Evolution em estado {evolution_state or 'desconhecido'}."
            )
        except HTTPException as exc:
            connection.status = CONNECTION_STATUS_NEEDS_RECONNECT
            connection.last_error = str(exc.detail)
        except EvolutionApiError as exc:
            connection.status = CONNECTION_STATUS_NEEDS_RECONNECT
            connection.last_error = exc.message

        await self.db.commit()
        await self.db.refresh(connection)
        return connection

    def _instance_name(self, connection: PlatformWhatsAppConnection) -> str:
        stored = connection.evolution_instance_name
        if stored:
            return stored
        env_name = (get_settings().evolution_welcome_instance_name or "").strip()
        if env_name:
            return env_name
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nome da instância Evolution da plataforma não definido.",
        )

    def _api_key(self, connection: PlatformWhatsAppConnection) -> str:
        token = connection.encrypted_instance_api_key
        if not token:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Instância Evolution da plataforma sem credencial configurada.",
            )
        try:
            return decrypt_secret(token)
        except CredentialEncryptionError as exc:
            settings = get_settings()
            if settings.evolution_global_api_key:
                logger.warning(
                    "Could not decrypt platform Evolution credential; "
                    "falling back to EVOLUTION_GLOBAL_API_KEY."
                )
                return settings.evolution_global_api_key
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Credencial WhatsApp da plataforma não pôde ser lida "
                    "(chave de criptografia alterada). Reconecte no painel admin."
                ),
            ) from exc

    def _apply_evolution_state(
        self,
        connection: PlatformWhatsAppConnection,
        evolution_state: str | None,
    ) -> str:
        mapped = _EVOLUTION_STATE_TO_CONNECTION.get(
            (evolution_state or "").lower(), CONNECTION_STATUS_SETUP_INCOMPLETE
        )
        connection.status = mapped
        if mapped == CONNECTION_STATUS_ACTIVE:
            connection.connected_at = connection.connected_at or datetime.now(UTC)
        return mapped

    async def _sync_phone_from_instances(
        self, connection: PlatformWhatsAppConnection, api_key: str
    ) -> None:
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
            break

    async def _ensure_webhook(self, instance_name: str, api_key: str) -> None:
        settings = get_settings()
        webhook_url = settings.evolution_webhook_url
        if not webhook_url:
            if settings.debug:
                logger.warning(
                    "APP_PUBLIC_URL not set; platform Evolution webhook not registered"
                )
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
                logger.warning("Failed to set platform Evolution webhook: %s", exc.message)
                return
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao registrar webhook Evolution: {exc.message}",
            ) from exc

    async def _remote_cleanup(self, connection: PlatformWhatsAppConnection) -> None:
        if not connection.evolution_instance_name:
            return
        try:
            api_key = self._api_key(connection)
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

    # ------------------------------------------------------------- welcome

    def resolve_welcome_message(self, connection: PlatformWhatsAppConnection) -> str:
        stored = (connection.welcome_message or "").strip()
        return stored or DEFAULT_WELCOME_MESSAGE

    async def set_welcome_message(
        self, connection: PlatformWhatsAppConnection, message: str | None
    ) -> None:
        connection.welcome_message = (message or "").strip() or None
        await self.db.commit()

    async def handle_webhook_event(self, payload: dict[str, Any]) -> bool:
        """Apply Evolution lifecycle events for the platform-owned instance.

        Returns ``True`` when the event belongs to the platform instance so the
        shared webhook dispatcher does not also route it as a professional event.
        """
        instance_name = payload.get("instance")
        if not instance_name:
            return False

        result = await self.db.execute(
            select(PlatformWhatsAppConnection).where(
                PlatformWhatsAppConnection.evolution_instance_name == str(instance_name)
            )
        )
        connection = result.scalars().first()
        if connection is None:
            return False

        event = normalize_evolution_event(str(payload.get("event") or ""))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if event in ("connection.update", "qrcode.updated"):
            state = None
            if isinstance(data, dict):
                state = data.get("state") or data.get("status")
            state = state or payload.get("state")
            if state:
                mapped = self._apply_evolution_state(connection, str(state))
                if mapped == CONNECTION_STATUS_ACTIVE:
                    connection.last_error = None

            if isinstance(data, dict):
                wuid = data.get("wuid") or data.get("ownerJid")
                if wuid:
                    connection.display_phone_number = str(wuid).split("@")[0]

        await self.db.commit()
        return True

    # ------------------------------------------------------------- lifecycle

    async def _connect_existing_instance(
        self,
        connection: PlatformWhatsAppConnection,
        *,
        instance_name: str,
        api_key: str,
        recover_missing: bool = True,
    ) -> PlatformConnectResult:
        try:
            state_payload = await self.client.connection_state(
                instance_name, api_key=api_key
            )
            evolution_state = self.client.extract_connection_state(state_payload)
            self._apply_evolution_state(connection, evolution_state)
            qrcode_base64 = None
            if evolution_state in (None, "close", "closed", "connecting"):
                connect_payload = await self.client.connect_instance(
                    instance_name, api_key=api_key
                )
                qrcode_base64 = self.client.extract_qrcode_base64(connect_payload)
            await self._ensure_webhook(instance_name, api_key)
            await self._sync_phone_from_instances(connection, api_key)
            await self.db.commit()
            await self.db.refresh(connection)
            return PlatformConnectResult(
                connection=connection,
                qrcode_base64=qrcode_base64,
                connection_state=evolution_state,
            )
        except HTTPException:
            raise
        except EvolutionApiError as exc:
            if recover_missing and self._is_missing_instance_error(exc):
                return await self._recover_missing_instance(
                    connection, missing_instance_name=instance_name
                )
            connection.last_error = exc.message
            await self.db.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao conectar instância Evolution: {exc.message}",
            ) from exc

    @staticmethod
    def _is_missing_instance_error(exc: EvolutionApiError) -> bool:
        message = exc.message.lower()
        return exc.status_code == 404 or (
            "instance" in message
            and ("does not exist" in message or "not found" in message)
        )

    async def _recover_missing_instance(
        self,
        connection: PlatformWhatsAppConnection,
        *,
        missing_instance_name: str,
    ) -> PlatformConnectResult:
        """Replace a stale local binding when Evolution no longer has it."""
        logger.warning(
            "Platform Evolution instance %s no longer exists; recreating stable instance",
            missing_instance_name,
        )
        connection.evolution_instance_name = None
        connection.encrypted_instance_api_key = None
        connection.display_phone_number = None
        connection.status = CONNECTION_STATUS_NOT_CONNECTED
        connection.connected_at = None
        connection.disconnected_at = None
        connection.last_error = None
        await self.db.flush()
        return await self.connect()

    async def connect(self) -> PlatformConnectResult:
        connection = await self.get_connection()
        instance_name = connection.evolution_instance_name

        if instance_name:
            # Reuse the stored instance: check state, reconnect if needed.
            try:
                api_key = self._api_key(connection)
            except HTTPException:
                api_key = get_settings().evolution_global_api_key
            return await self._connect_existing_instance(
                connection,
                instance_name=instance_name,
                api_key=api_key,
            )

        # New instance: name from env or generated; persisted for future reconnects.
        env_name = (get_settings().evolution_welcome_instance_name or "").strip()
        instance_name = env_name or "korus-welcome"
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
                global_key = settings.evolution_global_api_key
                if not global_key:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=(
                            "Instância Evolution já existe e não há credencial "
                            "para reutilizá-la. Configure EVOLUTION_GLOBAL_API_KEY "
                            "ou limpe a instância no painel Evolution."
                        ),
                    ) from exc
                connection.evolution_instance_name = instance_name
                connection.encrypted_instance_api_key = encrypt_secret(global_key)
                connection.last_error = None
                await self.db.flush()
                return await self._connect_existing_instance(
                    connection,
                    instance_name=instance_name,
                    api_key=global_key,
                    recover_missing=False,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Falha ao criar instância Evolution: {exc.message}",
                ) from exc

        api_key = self.client.extract_instance_api_key(created)
        if not api_key:
            api_key = settings.evolution_global_api_key
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Evolution não retornou apikey da instância da plataforma.",
                )

        connection.evolution_instance_name = instance_name
        connection.encrypted_instance_api_key = encrypt_secret(api_key)
        connection.status = CONNECTION_STATUS_CONNECTING
        connection.last_error = None
        await self.db.flush()

        await self._ensure_webhook(instance_name, api_key)

        qrcode_base64 = self.client.extract_qrcode_base64(created)
        evolution_state = "connecting"
        try:
            if not qrcode_base64:
                connect_payload = await self.client.connect_instance(
                    instance_name, api_key=api_key
                )
                qrcode_base64 = self.client.extract_qrcode_base64(connect_payload)
            state_payload = await self.client.connection_state(
                instance_name, api_key=api_key
            )
            evolution_state = (
                self.client.extract_connection_state(state_payload) or evolution_state
            )
            self._apply_evolution_state(connection, evolution_state)
        except EvolutionApiError as exc:
            connection.last_error = exc.message
            connection.status = CONNECTION_STATUS_SETUP_INCOMPLETE

        await self.db.commit()
        await self.db.refresh(connection)
        return PlatformConnectResult(
            connection=connection,
            qrcode_base64=qrcode_base64,
            connection_state=evolution_state,
        )

    async def refresh_connection(self) -> PlatformConnectResult:
        connection = await self.get_connection()
        if not connection.evolution_instance_name:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Nenhuma conexão WhatsApp Evolution da plataforma encontrada.",
            )
        api_key = self._api_key(connection)
        instance_name = self._instance_name(connection)
        qrcode_base64 = None
        try:
            state_payload = await self.client.connection_state(
                instance_name, api_key=api_key
            )
            evolution_state = self.client.extract_connection_state(state_payload)
            self._apply_evolution_state(connection, evolution_state)
            if evolution_state == "connecting":
                connect_payload = await self.client.connect_instance(
                    instance_name, api_key=api_key
                )
                qrcode_base64 = self.client.extract_qrcode_base64(connect_payload)
            await self._ensure_webhook(instance_name, api_key)
            await self._sync_phone_from_instances(connection, api_key)
        except HTTPException:
            raise
        except EvolutionApiError as exc:
            if self._is_missing_instance_error(exc):
                return await self._recover_missing_instance(
                    connection, missing_instance_name=instance_name
                )
            connection.last_error = exc.message
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha ao atualizar status Evolution: {exc.message}",
            ) from exc
        await self.db.commit()
        await self.db.refresh(connection)
        return PlatformConnectResult(
            connection=connection,
            qrcode_base64=qrcode_base64,
            connection_state=evolution_state,
        )

    async def disconnect(self) -> PlatformWhatsAppConnection:
        connection = await self.get_connection()
        if not connection.evolution_instance_name:
            return connection
        await self._remote_cleanup(connection)
        connection.status = CONNECTION_STATUS_DISCONNECTED
        connection.disconnected_at = datetime.now(UTC)
        await self.db.commit()
        await self.db.refresh(connection)
        return connection

    # ---------------------------------------------------------------- sending

    async def resolve_recipient_number(
        self, connection: PlatformWhatsAppConnection, recipient_phone: str
    ) -> str:
        """Normalize the number and validate it has WhatsApp (best-effort).

        Raises ``HTTPException`` when the phone is invalid or has no WhatsApp;
        falls back to the first candidate when the existence check fails.
        """
        instance_name = self._instance_name(connection)
        api_key = self._api_key(connection)
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
                "ou está incorreto."
            ),
        )

    async def send_text(
        self, connection: PlatformWhatsAppConnection, recipient_phone: str, text: str
    ) -> WhatsAppSendResult:
        api_key = self._api_key(connection)
        instance_name = self._instance_name(connection)
        number = await self.resolve_recipient_number(connection, recipient_phone)
        try:
            response = await self.client.send_text(
                instance_name, number, text, api_key=api_key
            )
        except EvolutionApiError as exc:
            if "close" in exc.message.lower() or exc.status_code == 401:
                connection.status = CONNECTION_STATUS_NEEDS_RECONNECT
                connection.last_error = exc.message
                await self.db.commit()
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
