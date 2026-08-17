"""Per-professional notification settings."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import (
    merge_whatsapp_events,
    merge_whatsapp_message_templates,
    normalize_whatsapp_events,
    normalize_whatsapp_message_templates,
)
from app.models.notification_settings import NotificationSettings


class NotificationSettingsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create(self, professional_id: UUID) -> NotificationSettings:
        result = await self.db.execute(
            select(NotificationSettings).where(NotificationSettings.professional_id == professional_id)
        )
        settings = result.scalar_one_or_none()
        if settings:
            return settings

        settings = NotificationSettings(
            professional_id=professional_id,
            whatsapp_enabled=False,
            appointment_confirmation_link_enabled=False,
            whatsapp_events=normalize_whatsapp_events(None),
            whatsapp_message_templates=normalize_whatsapp_message_templates(None),
        )
        self.db.add(settings)
        await self.db.flush()
        return settings

    async def update(
        self,
        professional_id: UUID,
        *,
        whatsapp_enabled: bool | None = None,
        appointment_confirmation_link_enabled: bool | None = None,
        whatsapp_events: dict[str, bool | None] | None = None,
        whatsapp_message_templates: dict[str, str | None] | None = None,
    ) -> NotificationSettings:
        settings = await self.get_or_create(professional_id)

        if whatsapp_enabled is not None:
            settings.whatsapp_enabled = whatsapp_enabled

        if appointment_confirmation_link_enabled is not None:
            settings.appointment_confirmation_link_enabled = (
                appointment_confirmation_link_enabled
            )

        if whatsapp_events is not None:
            current = normalize_whatsapp_events(settings.whatsapp_events)
            settings.whatsapp_events = merge_whatsapp_events(current, whatsapp_events)

        if whatsapp_message_templates is not None:
            current = normalize_whatsapp_message_templates(settings.whatsapp_message_templates)
            settings.whatsapp_message_templates = merge_whatsapp_message_templates(
                current, whatsapp_message_templates
            )

        await self.db.flush()
        return settings
