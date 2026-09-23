"""Digital pre-attendance intake records and private upload metadata."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_uuid

INTAKE_FORM_VERSION = "pediatric-v1"
INTAKE_DRAFT = "draft"
INTAKE_SUBMITTED = "submitted"
INTAKE_REVIEWED = "reviewed"
INTAKE_CANCELLED = "cancelled"
INTAKE_STATUSES = (INTAKE_DRAFT, INTAKE_SUBMITTED, INTAKE_REVIEWED, INTAKE_CANCELLED)


class IntakeRequest(Base, TimestampMixin):
    __tablename__ = "intake_requests"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_intake_requests_version"),
        CheckConstraint("status IN ('draft','submitted','reviewed','cancelled')", name="ck_intake_requests_status"),
        Index(
            "uq_intake_requests_open_caregiver",
            "patient_id", "caregiver_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    caregiver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("caregivers.id", ondelete="SET NULL"), nullable=True, index=True)
    caregiver_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="Responsável")
    owner_professional_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True)
    form_version: Mapped[str] = mapped_column(String(32), nullable=False, default=INTAKE_FORM_VERSION)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=INTAKE_DRAFT)
    responses: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True)
    submitted_command_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_command_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    anamnese_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    selected_file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class IntakeGrant(Base, TimestampMixin):
    __tablename__ = "intake_grants"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_intake_grants_expiry"),
        Index(
            "uq_intake_grants_active_request",
            "intake_request_id", unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    intake_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intake_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    caregiver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("caregivers.id", ondelete="SET NULL"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorization: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False)


class IntakeFile(Base, TimestampMixin):
    __tablename__ = "intake_files"
    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 5242880", name="ck_intake_files_size"),
        CheckConstraint("content_type IN ('application/pdf','image/jpeg','image/png')", name="ck_intake_files_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    intake_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intake_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    caregiver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("caregivers.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    incorporated_attachment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("attachments.id", ondelete="SET NULL"), nullable=True)


class IntakeAuditEvent(Base):
    __tablename__ = "intake_audit_events"
    __table_args__ = (Index("ix_intake_audit_events_request_time", "intake_request_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    intake_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intake_requests.id", ondelete="CASCADE"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_professional_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True)
    actor_caregiver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("caregivers.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
