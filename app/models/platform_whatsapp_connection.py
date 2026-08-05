"""Platform-owned WhatsApp connection (Evolution).

Singleton row used to send system messages from the platform's own number
(registration welcome messages, future product notifications). Managed from
the admin panel; not tied to any professional.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid
from app.models.whatsapp_connection import CONNECTION_STATUS_NOT_CONNECTED


class PlatformWhatsAppConnection(Base, TimestampMixin):
    __tablename__ = "platform_whatsapp_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CONNECTION_STATUS_NOT_CONNECTED
    )
    evolution_instance_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_instance_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Custom welcome message (NULL = default from whatsapp_events constants).
    welcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
