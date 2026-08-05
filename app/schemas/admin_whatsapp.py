"""Schemas for the admin WhatsApp platform endpoints (welcome message)."""

from pydantic import Field

from app.constants.whatsapp_events import WELCOME_MESSAGE_MAX_LENGTH
from app.schemas.common import CamelModel


class WhatsAppWelcomeMessageResponse(CamelModel):
    # Custom stored message; None = default copy in use.
    message: str | None = None
    default_message: str


class WhatsAppWelcomeMessageUpdate(CamelModel):
    # Empty/whitespace is treated as "reset to default".
    message: str | None = Field(default=None, max_length=WELCOME_MESSAGE_MAX_LENGTH)
