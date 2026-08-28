from datetime import datetime

from app.schemas.common import CamelModel


class GoogleCalendarStatusResponse(CamelModel):
    configured: bool
    connected: bool
    include_patient_name: bool = False
    connected_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    pending_events: int = 0
    failed_events: int = 0


class GoogleCalendarAuthorizationResponse(CamelModel):
    authorization_url: str


class GoogleCalendarSettingsUpdate(CamelModel):
    include_patient_name: bool


class GoogleCalendarSyncResponse(CamelModel):
    queued_count: int
    message: str
