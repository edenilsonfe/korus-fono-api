from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, EmailStr, Field

from app.schemas.common import CamelModel

ReportDeliveryChannel = Literal["link", "whatsapp", "email"]
# standard = F1 delivery; school = F20 school delivery with recorded authorization.
ReportDeliveryRecipientKind = Literal["standard", "school"]
# fixed = document frozen in ReportDelivery.document_snapshot (F3);
# legacy_live = row created before the snapshot existed, read from the report.
ReportDeliverySnapshotMode = Literal["fixed", "legacy_live"]


class SchoolRecipient(CamelModel):
    """Minimal school recipient: institution + declared contact, nothing clinical."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    recipient_name: str = Field(min_length=1, max_length=160)


class SchoolDeliveryAuthorization(CamelModel):
    """Professional declaration kept with the delivery: who authorized it, when and
    on which evidence. At least one evidence is required; the server records the
    actor professional and its own timestamp."""

    model_config = ConfigDict(extra="forbid")

    caregiver_id: UUID
    authorized_at: datetime
    evidence_reference: str | None = Field(default=None, max_length=500)
    evidence_attachment_id: UUID | None = None
    reviewed: bool


class ReportDeliveryCreate(CamelModel):
    model_config = ConfigDict(extra="forbid")

    channel: ReportDeliveryChannel
    caregiver_id: str | None = None
    email: EmailStr | None = None
    expires_in_days: int = Field(default=30, ge=1, le=180)
    recipient_kind: ReportDeliveryRecipientKind = "standard"
    school: SchoolRecipient | None = None
    school_authorization: SchoolDeliveryAuthorization | None = None


class SchoolAuthorizationRecord(CamelModel):
    """Restricted authorization record exposed only in the owner's authenticated
    response — the public DTO never carries it."""

    caregiver_id: str
    authorized_at: datetime
    evidence_reference: str | None = None
    evidence_attachment_id: str | None = None
    reviewed: bool = True
    purpose: str = "school_report"
    recorded_at: datetime | None = None
    recorded_by_professional_id: str | None = None


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
    report_version: int | None = None
    content_hash: str | None = None
    snapshot_mode: ReportDeliverySnapshotMode = "legacy_live"
    # F20 — school fields (null for standard deliveries).
    recipient_kind: str = "standard"
    school_name: str | None = None
    school_recipient_name: str | None = None
    school_authorization: SchoolAuthorizationRecord | None = None
    authorization_recorded_at: datetime | None = None
    received_at: datetime | None = None
    received_by_name: str | None = None
    received_by_role: str | None = None


class PublicReportDeliveryResponse(CamelModel):
    report_type: str
    report_type_label: str
    patient_name: str
    professional_name: str
    professional_council: str = ""
    date: str
    content: str
    expires_at: datetime
    report_version: int | None = None
    content_hash: str | None = None
    snapshot_mode: ReportDeliverySnapshotMode = "legacy_live"
    # F20 — recipient kind and receipt state. `requires_acknowledgement` marks a
    # school delivery (the kind that accepts an explicit receipt); `received_at`
    # is the stored receipt. Reading never confirms. Never expose the school
    # contact, the caregiver or the authorization evidence here.
    recipient_kind: str = "standard"
    requires_acknowledgement: bool = False
    received_at: datetime | None = None


class ReportReceiptCreate(CamelModel):
    """Public acknowledgement payload (F20): self-declared identity only.

    Never carries the server timestamp, the school contact or clinical data —
    `extra=forbid` rejects any attempt to set `receivedAt` from the client.
    """

    model_config = ConfigDict(extra="forbid")

    received: bool
    receiver_name: str = Field(min_length=1, max_length=160)
    receiver_role: str = Field(min_length=1, max_length=120)


class ReportReceiptResponse(CamelModel):
    """Stored receipt returned to the school: server timestamp plus the
    delivered document identifiers. A replay returns the first registration."""

    received_at: datetime
    report_version: int | None = None
    content_hash: str | None = None
