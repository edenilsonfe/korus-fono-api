from datetime import date, datetime, time
from typing import Any

from app.schemas.common import CamelModel


class WhatsAppMessageLogItem(CamelModel):
    id: str
    appointment_id: str | None = None
    created_at: datetime
    notification_type: str
    event_label: str
    recipient_name: str | None = None
    recipient_phone: str | None = None
    template_name: str | None = None
    status: str
    status_label: str
    delivery_seconds: int | None = None
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    attempt_count: int = 0
    provider_message_id: str | None = None
    skip_reason: str | None = None
    dispatch_decision: dict[str, Any] | None = None
    last_error: str | None = None
    is_test: bool = False


class WhatsAppMessageLogsResponse(CamelModel):
    items: list[WhatsAppMessageLogItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class WhatsAppMessageLogsStatsResponse(CamelModel):
    period_days: int = 30
    sent: int = 0
    delivered: int = 0
    queued: int = 0
    skipped: int = 0
    failed: int = 0
    no_phone: int = 0
    total: int = 0
