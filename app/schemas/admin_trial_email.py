from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import CamelModel

TrialEmailAudience = Literal["expired", "expiring_soon"]
TrialEmailCampaignStatus = Literal["pending", "processing", "completed", "failed"]


class TrialEmailRecipientPreview(CamelModel):
    id: str
    name: str
    email: str
    trial_ends_at: datetime


class TrialEmailPreview(CamelModel):
    audience: TrialEmailAudience
    expires_within_days: int | None = None
    eligible_count: int
    suppressed_count: int
    sample: list[TrialEmailRecipientPreview] = Field(default_factory=list)
    subject: str


class TrialEmailCampaignCreate(CamelModel):
    audience: TrialEmailAudience
    expires_within_days: int | None = Field(default=3, ge=1, le=30)

    @model_validator(mode="after")
    def normalize_window(self):
        if self.audience == "expired":
            self.expires_within_days = None
        elif self.expires_within_days is None:
            self.expires_within_days = 3
        return self


class TrialEmailCampaignResponse(CamelModel):
    id: str
    audience: TrialEmailAudience
    expires_within_days: int | None = None
    status: TrialEmailCampaignStatus
    eligible_count: int
    suppressed_count: int
    sent_count: int
    failed_count: int
    skipped_count: int
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
