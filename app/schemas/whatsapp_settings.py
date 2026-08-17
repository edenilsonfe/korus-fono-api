from pydantic import Field

from app.constants.whatsapp_events import DEFAULT_WHATSAPP_EVENTS
from app.schemas.common import CamelModel


class WhatsAppEventSettings(CamelModel):
    appointment_reminder_24h: bool = False
    appointment_confirmation: bool = False
    appointment_cancelled: bool = False
    appointment_rescheduled: bool = False
    billing_reminder: bool = False
    billing_overdue: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "WhatsAppEventSettings":
        merged = dict(DEFAULT_WHATSAPP_EVENTS)
        if isinstance(raw, dict):
            for key in DEFAULT_WHATSAPP_EVENTS:
                if key in raw:
                    merged[key] = bool(raw[key])
        return cls(**merged)


class WhatsAppEventSettingsUpdate(CamelModel):
    appointment_reminder_24h: bool | None = None
    appointment_confirmation: bool | None = None
    appointment_cancelled: bool | None = None
    appointment_rescheduled: bool | None = None
    billing_reminder: bool | None = None
    billing_overdue: bool | None = None

    def to_update_dict(self) -> dict[str, bool]:
        return {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if value is not None
        }


class WhatsAppMessageTemplates(CamelModel):
    appointment_reminder_24h: str | None = None
    appointment_confirmation: str | None = None
    appointment_cancelled: str | None = None
    appointment_rescheduled: str | None = None
    billing_reminder: str | None = None
    billing_overdue: str | None = None


class WhatsAppMessageTemplatesUpdate(CamelModel):
    appointment_reminder_24h: str | None = Field(default=None)
    appointment_confirmation: str | None = Field(default=None)
    appointment_cancelled: str | None = Field(default=None)
    appointment_rescheduled: str | None = Field(default=None)
    billing_reminder: str | None = Field(default=None)
    billing_overdue: str | None = Field(default=None)

    def to_update_dict(self) -> dict[str, str | None]:
        from app.constants.whatsapp_events import WHATSAPP_EVENT_IDS

        return {
            key: value
            for key, value in self.model_dump(exclude_unset=True).items()
            if key in WHATSAPP_EVENT_IDS
        }


class WhatsAppSettingsResponse(CamelModel):
    whatsapp_enabled: bool
    appointment_confirmation_link_enabled: bool
    whatsapp_events: WhatsAppEventSettings
    whatsapp_message_templates: dict[str, str | None]
    template_defaults: dict[str, str]


class WhatsAppSettingsUpdate(CamelModel):
    whatsapp_enabled: bool | None = None
    appointment_confirmation_link_enabled: bool | None = None
    whatsapp_events: WhatsAppEventSettingsUpdate | None = None
    whatsapp_message_templates: WhatsAppMessageTemplatesUpdate | None = None
