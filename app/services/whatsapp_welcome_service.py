"""WhatsApp welcome message sent to a user right after account creation.

The message is sent from the platform's own Evolution number (see
``PlatformWhatsAppService``), managed in the admin panel. The copy can be
customized there; when no custom text is stored, ``DEFAULT_WELCOME_MESSAGE``
is used. Every registration first creates a durable ``NotificationMessageLog``.
The provider message id is reconciled by webhook and failed deliveries are
retried by the WhatsApp scheduler.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import DEFAULT_WELCOME_MESSAGE
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.notification_message_log import (
    MESSAGE_STATUS_DELIVERED,
    MESSAGE_STATUS_FAILED,
    MESSAGE_STATUS_QUEUED,
    MESSAGE_STATUS_READ,
    MESSAGE_STATUS_SENT,
    NotificationMessageLog,
)
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.professional import Professional
from app.models.whatsapp_connection import CONNECTION_STATUS_ACTIVE
from app.services.evolution_api_client import EvolutionApiError
from app.services.evolution_whatsapp_service import mask_phone
from app.services.platform_whatsapp_service import PlatformWhatsAppService
from app.services.whatsapp_types import WhatsAppSendResult
from app.services.whatsapp_welcome_policy import (
    MAX_WELCOME_SEND_ATTEMPTS,
    WELCOME_NOTIFICATION_TYPE,
    welcome_retry_at,
)

logger = logging.getLogger(__name__)

# Re-exported so callers/tests can keep importing from this module.
WELCOME_MESSAGE = DEFAULT_WELCOME_MESSAGE

_PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")
_DONE_STATUSES = frozenset(
    {MESSAGE_STATUS_SENT, MESSAGE_STATUS_DELIVERED, MESSAGE_STATUS_READ}
)
_CLAIM_TIMEOUT = timedelta(minutes=5)


class WelcomeDispatchError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        consumes_attempt: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.consumes_attempt = consumes_attempt


def _first_name(full_name: str | None) -> str:
    name = (full_name or "").strip()
    return name.split()[0] if name else ""


def _render_welcome_message(template: str, first_name: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        return first_name if match.group(1) == "firstName" else match.group(0)

    return _PLACEHOLDER_PATTERN.sub(replacer, template)


def _welcome_deduplication_key(professional_id: UUID) -> str:
    return f"registration-welcome:{professional_id}"


async def queue_whatsapp_welcome_message(
    db: AsyncSession, professional: Professional
) -> NotificationMessageLog:
    """Persist one durable welcome dispatch per registered professional."""
    deduplication_key = _welcome_deduplication_key(professional.id)
    existing = await db.scalar(
        select(NotificationMessageLog).where(
            NotificationMessageLog.deduplication_key == deduplication_key
        )
    )
    if existing is not None:
        return existing

    log = NotificationMessageLog(
        professional_id=professional.id,
        channel="whatsapp",
        notification_type=WELCOME_NOTIFICATION_TYPE,
        provider="evolution",
        deduplication_key=deduplication_key,
        to_phone=mask_phone(professional.phone),
        status=MESSAGE_STATUS_QUEUED,
        attempt_count=0,
        is_test=False,
    )
    db.add(log)
    await db.flush()
    return log


async def _send_with_session(
    db: AsyncSession, *, user_name: str, phone: str
) -> tuple[WhatsAppSendResult, PlatformWhatsAppConnection]:
    settings = get_settings()
    if settings.whatsapp_provider != "evolution":
        raise WelcomeDispatchError(
            "provider_disabled",
            f"Provedor WhatsApp ativo é {settings.whatsapp_provider!r}.",
            retryable=False,
        )

    phone = (phone or "").strip()
    if not phone:
        raise WelcomeDispatchError(
            "missing_phone", "Telefone não informado.", retryable=False
        )

    result = await db.execute(select(PlatformWhatsAppConnection).limit(1))
    connection = result.scalars().first()
    service = PlatformWhatsAppService(db)
    if connection is not None and connection.evolution_instance_name:
        connection = await service.reconcile_status(connection)
    if (
        connection is None
        or connection.status != CONNECTION_STATUS_ACTIVE
        or not connection.evolution_instance_name
    ):
        raise WelcomeDispatchError(
            "connection_not_active",
            "Conexão WhatsApp da plataforma não está ativa.",
            retryable=True,
            consumes_attempt=False,
        )

    template = service.resolve_welcome_message(connection)
    text = _render_welcome_message(template, _first_name(user_name))
    send_result = await service.send_text(connection, phone, text)
    return send_result, connection


async def _mark_dispatch_failed(
    db: AsyncSession,
    log: NotificationMessageLog,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    now = datetime.now(UTC)
    log.status = MESSAGE_STATUS_FAILED
    log.error_code = code
    log.last_error = message
    log.failed_at = now
    log.next_retry_at = (
        welcome_retry_at(log.attempt_count, now=now)
        if retryable and log.attempt_count < MAX_WELCOME_SEND_ATTEMPTS
        else None
    )
    await db.commit()


async def dispatch_whatsapp_welcome_message(log_id: UUID) -> bool:
    """Claim and dispatch one persisted welcome message attempt."""
    async with AsyncSessionLocal() as db:
        log = await db.scalar(
            select(NotificationMessageLog)
            .where(
                NotificationMessageLog.id == log_id,
                NotificationMessageLog.notification_type
                == WELCOME_NOTIFICATION_TYPE,
            )
            .with_for_update()
        )
        if log is None or log.status in _DONE_STATUSES:
            return False
        if log.attempt_count >= MAX_WELCOME_SEND_ATTEMPTS:
            return False

        now = datetime.now(UTC)
        if log.status == MESSAGE_STATUS_FAILED:
            next_retry_at = log.next_retry_at
            if next_retry_at is not None and next_retry_at.tzinfo is None:
                next_retry_at = next_retry_at.replace(tzinfo=UTC)
            if next_retry_at is None or next_retry_at > now:
                return False
        elif log.status == MESSAGE_STATUS_QUEUED and log.attempt_count:
            stamped = log.updated_at or log.created_at
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=UTC)
            if now - stamped < _CLAIM_TIMEOUT:
                return False

        professional = (
            await db.get(Professional, log.professional_id)
            if log.professional_id is not None
            else None
        )
        if professional is None:
            await _mark_dispatch_failed(
                db,
                log,
                code="professional_missing",
                message="Profissional do cadastro não encontrado.",
                retryable=False,
            )
            return False

        log.status = MESSAGE_STATUS_QUEUED
        log.attempt_count = int(log.attempt_count or 0) + 1
        log.next_retry_at = None
        log.error_code = None
        log.last_error = None
        log.failed_at = None
        log.provider_message_id = None
        await db.commit()

        try:
            send_result, connection = await _send_with_session(
                db, user_name=professional.name, phone=professional.phone
            )
        except WelcomeDispatchError as exc:
            if not exc.consumes_attempt:
                log.attempt_count = max(0, log.attempt_count - 1)
            await _mark_dispatch_failed(
                db,
                log,
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
            logger.warning(
                "WhatsApp welcome attempt failed for %s: %s",
                professional.id,
                exc.message,
            )
            return False
        except HTTPException as exc:
            message = str(exc.detail)
            permanent = exc.status_code == 400
            await _mark_dispatch_failed(
                db,
                log,
                code="invalid_phone" if permanent else "provider_error",
                message=message,
                retryable=not permanent,
            )
            return False
        except EvolutionApiError as exc:
            await _mark_dispatch_failed(
                db,
                log,
                code="provider_error",
                message=exc.message,
                retryable=True,
            )
            return False
        except Exception as exc:
            logger.exception("Unexpected WhatsApp welcome dispatch failure")
            await _mark_dispatch_failed(
                db,
                log,
                code="unexpected_error",
                message=str(exc),
                retryable=True,
            )
            return False

        log.status = MESSAGE_STATUS_SENT
        log.provider = send_result.provider
        log.provider_message_id = send_result.provider_message_id
        log.payload = send_result.payload
        log.sent_at = datetime.now(UTC)
        log.next_retry_at = None
        await db.commit()
        logger.info(
            "WhatsApp welcome accepted for %s via %s (message %s)",
            professional.id,
            connection.evolution_instance_name,
            send_result.provider_message_id or "unknown",
        )
        return True


async def retry_due_whatsapp_welcome_messages() -> int:
    """Dispatch durable queued welcomes and retry provider delivery failures."""
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        result = await db.scalars(
            select(NotificationMessageLog.id).where(
                NotificationMessageLog.notification_type
                == WELCOME_NOTIFICATION_TYPE,
                NotificationMessageLog.attempt_count
                < MAX_WELCOME_SEND_ATTEMPTS,
                or_(
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_QUEUED,
                        or_(
                            NotificationMessageLog.attempt_count == 0,
                            NotificationMessageLog.updated_at
                            <= now - _CLAIM_TIMEOUT,
                        ),
                    ),
                    and_(
                        NotificationMessageLog.status == MESSAGE_STATUS_FAILED,
                        NotificationMessageLog.next_retry_at.is_not(None),
                        NotificationMessageLog.next_retry_at <= now,
                    ),
                ),
            )
        )
        log_ids = list(result.all())

    dispatched = 0
    for log_id in log_ids:
        if await dispatch_whatsapp_welcome_message(log_id):
            dispatched += 1
    return dispatched


async def send_whatsapp_welcome_message(*, user_name: str, phone: str) -> bool:
    """Direct compatibility helper used by focused provider checks.

    Registration uses the durable queue above. This helper returns True only
    when the provider accepted the request and never raises to callers.
    """
    async with AsyncSessionLocal() as db:
        try:
            send_result, connection = await _send_with_session(
                db, user_name=user_name, phone=phone
            )
        except (WelcomeDispatchError, HTTPException, EvolutionApiError) as exc:
            message = (
                getattr(exc, "detail", None)
                or getattr(exc, "message", None)
                or str(exc)
            )
            logger.warning(
                "WhatsApp welcome not sent to %s: %s",
                user_name or "user",
                message,
            )
            return False
        except Exception:
            logger.exception(
                "WhatsApp welcome failed unexpectedly for %s",
                user_name or "user",
            )
            return False

    logger.info(
        "WhatsApp welcome accepted for %s (%s) via %s (message %s)",
        user_name or "user",
        mask_phone(phone),
        connection.evolution_instance_name,
        send_result.provider_message_id or "unknown",
    )
    return True
