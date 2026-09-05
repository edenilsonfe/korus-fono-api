import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class NotificationSettings(Base, TimestampMixin):
    __tablename__ = "notification_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    appointment_confirmation_deadline_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    birthday_in_app_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    appointment_confirmation_link_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    whatsapp_events: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    whatsapp_message_templates: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
