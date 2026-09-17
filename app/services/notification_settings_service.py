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
from app.core.utils import utcnow
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
        birthday_in_app_enabled: bool | None = None,
        weekly_summary_email_enabled: bool | None = None,
        appointment_confirmation_link_enabled: bool | None = None,
        appointment_confirmation_deadline_time: str | None = None,
        update_confirmation_deadline: bool = False,
        reassessment_reminder_months: int | None = None,
        update_reassessment_months: bool = False,
        no_show_policy: str | None = None,
        update_no_show_policy: bool = False,
        whatsapp_events: dict[str, bool | None] | None = None,
        whatsapp_message_templates: dict[str, str | None] | None = None,
    ) -> NotificationSettings:
        settings = await self.get_or_create(professional_id)

        if update_confirmation_deadline:
            settings.appointment_confirmation_deadline_time = appointment_confirmation_deadline_time

        if update_reassessment_months:
            settings.reassessment_reminder_months = reassessment_reminder_months

        if update_no_show_policy:
            settings.no_show_policy = no_show_policy

        if birthday_in_app_enabled is not None:
            settings.birthday_in_app_enabled = birthday_in_app_enabled

        if weekly_summary_email_enabled is not None:
            was_enabled = settings.weekly_summary_email_enabled
            settings.weekly_summary_email_enabled = weekly_summary_email_enabled
            settings.weekly_summary_email_preference_source = "settings"
            if weekly_summary_email_enabled:
                if not was_enabled or settings.weekly_summary_email_opted_in_at is None:
                    settings.weekly_summary_email_opted_in_at = utcnow()
                settings.weekly_summary_email_opted_out_at = None
                settings.weekly_summary_email_suppressed_at = None
                settings.weekly_summary_email_suppression_reason = None
            elif was_enabled or settings.weekly_summary_email_opted_out_at is None:
                settings.weekly_summary_email_opted_out_at = utcnow()

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
