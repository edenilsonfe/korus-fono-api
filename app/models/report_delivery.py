import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_uuid

DELIVERY_CHANNEL_LINK = "link"
DELIVERY_CHANNEL_WHATSAPP = "whatsapp"
DELIVERY_CHANNEL_EMAIL = "email"
DELIVERY_CHANNELS = frozenset(
    {DELIVERY_CHANNEL_LINK, DELIVERY_CHANNEL_WHATSAPP, DELIVERY_CHANNEL_EMAIL}
)

DELIVERY_STATUS_CREATED = "created"
DELIVERY_STATUS_SENT = "sent"
DELIVERY_STATUS_FAILED = "failed"

# F20 — recipient kind. `standard` is the F1 delivery (caregiver/avulso recipient);
# `school` adds the minimal school recipient and the restricted authorization record.
RECIPIENT_KIND_STANDARD = "standard"
RECIPIENT_KIND_SCHOOL = "school"
RECIPIENT_KINDS = frozenset({RECIPIENT_KIND_STANDARD, RECIPIENT_KIND_SCHOOL})


class ReportDelivery(Base, TimestampMixin):
    """Revocable, expiring delivery of a finalized AI report to a recipient."""

    __tablename__ = "report_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Only the SHA-256 hex digest of the raw token is persisted.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_STATUS_CREATED
    )
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Frozen delivery document (F3): clinical text + minimal textual metadata
    # captured when the delivery is created. Never stores the raw token, its URL,
    # protected answers or binary assets. Legacy rows stay NULL and are read live.
    document_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # F20 — school deliveries. `standard` keeps F1 behaviour; school rows carry the
    # minimal recipient data plus the restricted authorization record (authorizing
    # caregiver, at least one evidence, `reviewed`, server purpose). Receipt fields
    # stay NULL until the public acknowledgement flow (F20 fase 2) writes them.
    recipient_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RECIPIENT_KIND_STANDARD,
        server_default=RECIPIENT_KIND_STANDARD,
    )
    school_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    school_recipient_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    school_authorization: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    authorization_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_by_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    received_by_role: Mapped[str | None] = mapped_column(String(120), nullable=True)

    report: Mapped["AIReport"] = relationship()  # noqa: F821
