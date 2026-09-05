from pydantic import Field

from app.constants.whatsapp_events import DEFAULT_WHATSAPP_EVENTS
from app.schemas.common import CamelModel


class WhatsAppEventSettings(CamelModel):
    patient_birthday: bool = False
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
    patient_birthday: bool | None = None
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
    patient_birthday: str | None = None
    appointment_reminder_24h: str | None = None
    appointment_confirmation: str | None = None
    appointment_cancelled: str | None = None
    appointment_rescheduled: str | None = None
    billing_reminder: str | None = None
    billing_overdue: str | None = None


class WhatsAppMessageTemplatesUpdate(CamelModel):
    patient_birthday: str | None = Field(default=None, max_length=4000)
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
    appointment_confirmation_deadline_time: str | None = None
    whatsapp_enabled: bool
    appointment_confirmation_link_enabled: bool
    whatsapp_events: WhatsAppEventSettings
    whatsapp_message_templates: dict[str, str | None]
    template_defaults: dict[str, str]


class WhatsAppSettingsUpdate(CamelModel):
    appointment_confirmation_deadline_time: str | None = Field(
        default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
    )
    whatsapp_enabled: bool | None = None
    appointment_confirmation_link_enabled: bool | None = None
    whatsapp_events: WhatsAppEventSettingsUpdate | None = None
    whatsapp_message_templates: WhatsAppMessageTemplatesUpdate | None = None
