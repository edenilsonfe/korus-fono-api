import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class ClinicalReview(Base, TimestampMixin):
    """Immutable clinical decision document once completed.

    Draft fields are edited in place with optimistic ``version`` control. A
    correction is a new row pointing at the completed row through
    ``supersedes_review_id``.
    """

    __tablename__ = "clinical_reviews"
    __table_args__ = (
        CheckConstraint("kind IN ('periodic', 'discharge')", name="ck_clinical_review_kind"),
        CheckConstraint("status IN ('draft', 'completed', 'cancelled')", name="ck_clinical_review_status"),
        CheckConstraint("version >= 1", name="ck_clinical_review_version"),
        UniqueConstraint("id", "completion_idempotency_key", name="uq_clinical_review_completion_key"),
        Index("ix_clinical_reviews_patient_status", "patient_id", "status"),
        Index("ix_clinical_reviews_due", "patient_id", "next_review_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goal_decisions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_review_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    supersedes_review_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clinical_reviews.id", ondelete="SET NULL"), nullable=True, index=True
    )
    completion_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completion_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Discharge-only fields. They stay nullable so periodic reviews have no
    # fake clinical values.
    discharge_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    discharge_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    final_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    family_guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_recommended: Mapped[bool | None] = mapped_column(nullable=True)
    return_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    home_program_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    agenda_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    appointment_decisions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    family_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_reports.id", ondelete="SET NULL"), nullable=True
    )
