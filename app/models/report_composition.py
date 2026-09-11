import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class AIReportComposition(Base, TimestampMixin):
    """Restricted provenance snapshot for a consolidated (multi-instrument) report.

    Stores the selected source IDs, a minimal snapshot of the selected clinical
    data (no raw answers, no attachments) and the hashes that tie the persisted
    draft to the exact context the draft was generated from.
    """

    __tablename__ = "ai_report_compositions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_reports.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    selection: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_hashes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False, default="consolidado.v1")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_reports.id", ondelete="SET NULL"), nullable=True
    )
