import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class InterventionProgram(Base, TimestampMixin):
    __tablename__ = "intervention_programs"
    __table_args__ = (
        CheckConstraint("approach = 'aba'", name="ck_intervention_program_approach"),
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'mastered', 'archived')",
            name="ck_intervention_program_status",
        ),
        CheckConstraint(
            "mastery_percent BETWEEN 1 AND 100",
            name="ck_intervention_program_mastery_percent",
        ),
        CheckConstraint(
            "mastery_consecutive_sessions BETWEEN 1 AND 20",
            name="ck_intervention_program_mastery_sessions",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True
    )
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id"), nullable=True
    )
    approach: Mapped[str] = mapped_column(String(16), nullable=False, default="aba")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    operational_definition: Mapped[str] = mapped_column(Text, nullable=False)
    teaching_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    mastery_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    mastery_consecutive_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    generalization_criterion: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_criterion: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    replaces_program_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intervention_programs.id"), nullable=True
    )


class ProgramMeasurement(Base):
    __tablename__ = "program_measurements"
    __table_args__ = (
        CheckConstraint(
            "participation_status IN "
            "('participated', 'declined', 'withdrew', 'not_observed')",
            name="ck_program_measurement_participation",
        ),
        CheckConstraint(
            "review_status IN ('submitted', 'approved', 'voided')",
            name="ck_program_measurement_review_status",
        ),
        CheckConstraint(
            "opportunities >= 0 AND independent >= 0 AND prompted >= 0 "
            "AND incorrect >= 0 AND no_response >= 0",
            name="ck_program_measurement_nonnegative_counts",
        ),
        CheckConstraint(
            "(participation_status = 'participated' AND opportunities > 0 "
            "AND independent + prompted + incorrect + no_response = opportunities) "
            "OR (participation_status <> 'participated' AND opportunities = 0 "
            "AND independent = 0 AND prompted = 0 AND incorrect = 0 "
            "AND no_response = 0)",
            name="ck_program_measurement_consistent_counts",
        ),
        UniqueConstraint(
            "recorded_by_professional_id",
            "client_record_id",
            name="uq_program_measurement_client_record",
        ),
        UniqueConstraint(
            "replaces_measurement_id",
            name="uq_program_measurement_replacement",
        ),
        Index(
            "uq_program_measurement_current_session",
            "program_id",
            "session_id",
            unique=True,
            postgresql_where=text("review_status <> 'voided'"),
            sqlite_where=text("review_status <> 'voided'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    client_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intervention_programs.id"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True
    )
    recorded_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    participation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    opportunities: Mapped[int] = mapped_column(Integer, nullable=False)
    independent: Mapped[int] = mapped_column(Integer, nullable=False)
    prompted: Mapped[int] = mapped_column(Integer, nullable=False)
    incorrect: Mapped[int] = mapped_column(Integer, nullable=False)
    no_response: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_counts: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reviewed_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    replaces_measurement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("program_measurements.id"), nullable=True
    )
