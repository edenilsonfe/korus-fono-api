"""DTOs de marketing / tracking (Meta Pixel)."""

from typing import Any

from pydantic import Field

from app.schemas.common import CamelModel


class PixelConfigResponse(CamelModel):
    """Config pública para o snippet do Pixel no web (mesmo JSON em camelCase)."""

    pixel_id: str = Field(alias="pixelId")
    enabled: bool


class TrackEventRequest(CamelModel):
    """Evento a ser reenviado ao Conversions API (dedup com o evento do navegador)."""

    event_name: str = Field(alias="eventName")
    event_id: str = Field(alias="eventId")
    event_source_url: str | None = Field(default=None, alias="eventSourceUrl")
    custom_data: dict[str, Any] | None = Field(default=None, alias="customData")
    fbp: str | None = None
    fbc: str | None = None
