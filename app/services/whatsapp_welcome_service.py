"""WhatsApp welcome message sent to a user right after account creation.

The message is sent from the platform's own Evolution number (see
``PlatformWhatsAppService``), managed in the admin panel. The copy can be
customized there; when no custom text is stored, ``DEFAULT_WELCOME_MESSAGE``
is used. When the platform connection is not active, sending is skipped with
a log entry (same pattern as the new-account notification email).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.constants.whatsapp_events import DEFAULT_WELCOME_MESSAGE
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.whatsapp_connection import CONNECTION_STATUS_ACTIVE
from app.services.evolution_api_client import EvolutionApiError
from app.services.evolution_whatsapp_service import mask_phone
from app.services.platform_whatsapp_service import PlatformWhatsAppService

logger = logging.getLogger(__name__)

# Re-exported so callers/tests can keep importing from this module.
WELCOME_MESSAGE = DEFAULT_WELCOME_MESSAGE

_PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _first_name(full_name: str | None) -> str:
    name = (full_name or "").strip()
    return name.split()[0] if name else ""


def _render_welcome_message(template: str, first_name: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        return first_name if match.group(1) == "firstName" else match.group(0)

    return _PLACEHOLDER_PATTERN.sub(replacer, template)


async def send_whatsapp_welcome_message(*, user_name: str, phone: str) -> bool:
    """Send the welcome WhatsApp message to a new user (fire-and-forget).

    Returns True when the provider accepted the message. Never raises — skips
    (with a log) when the platform connection is not active, the phone is
    missing or the number has no WhatsApp.
    """
    settings = get_settings()
    if settings.whatsapp_provider != "evolution":
        logger.info(
            "WhatsApp welcome skipped: provider is %r",
            settings.whatsapp_provider,
        )
        return False

    phone = (phone or "").strip()
    if not phone:
        logger.info("WhatsApp welcome skipped for %s: phone not provided", user_name or "user")
        return False

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(PlatformWhatsAppConnection).limit(1)
            )
            connection = result.scalars().first()
            service = PlatformWhatsAppService(db)
            if connection is not None and connection.evolution_instance_name:
                connection = await service.reconcile_status(connection)
            if (
                connection is None
                or connection.status != CONNECTION_STATUS_ACTIVE
                or not connection.evolution_instance_name
            ):
                logger.info(
                    "WhatsApp welcome skipped for %s: platform connection not active",
                    user_name or "user",
                )
                return False

            number = await service.resolve_recipient_number(connection, phone)
            template = service.resolve_welcome_message(connection)
            text = _render_welcome_message(template, _first_name(user_name))
            send_result = await service.send_text(connection, number, text)
        except (HTTPException, EvolutionApiError) as exc:
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
        "WhatsApp welcome sent to %s (%s) via %s (message %s)",
        user_name or "user",
        mask_phone(phone),
        connection.evolution_instance_name,
        send_result.provider_message_id or "unknown",
    )
    return True
