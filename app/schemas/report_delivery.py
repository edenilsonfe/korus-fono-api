from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field

from app.schemas.common import CamelModel

ReportDeliveryChannel = Literal["link", "whatsapp", "email"]


class ReportDeliveryCreate(CamelModel):
    channel: ReportDeliveryChannel
    caregiver_id: str | None = None
    email: EmailStr | None = None
    expires_in_days: int = Field(default=30, ge=1, le=180)


class ReportDeliveryResponse(CamelModel):
    id: str
    report_id: str
    channel: str
    recipient_label: str
    status: str
    url: str | None = None
    expires_at: datetime
    revoked_at: datetime | None = None
    view_count: int = 0
    download_count: int = 0
    first_viewed_at: datetime | None = None
    last_viewed_at: datetime | None = None
    last_downloaded_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime


class PublicReportDeliveryResponse(CamelModel):
    report_type: str
    report_type_label: str
    patient_name: str
    professional_name: str
    professional_council: str = ""
    date: str
    content: str
    expires_at: datetime
