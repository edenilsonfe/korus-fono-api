import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_uuid


class Patient(Base, TimestampMixin):
    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    diagnosis_keys: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="avaliacao")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(64), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    therapy_plan_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    therapy_plan_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    anamnese_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    anamnese_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    professional: Mapped["Professional"] = relationship(back_populates="patients")  # noqa: F821
    caregivers: Mapped[list["Caregiver"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    goals: Mapped[list["Goal"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    sessions: Mapped[list["Session"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    assessments: Mapped[list["Assessment"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    evolutions: Mapped[list["Evolution"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    anamnese_entries: Mapped[list["AnamneseEntry"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    timeline_events: Mapped[list["TimelineEvent"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    domain_snapshots: Mapped[list["ClinicalDomainSnapshot"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
    ai_reports: Mapped[list["AIReport"]] = relationship(back_populates="patient", cascade="all, delete-orphan")  # noqa: F821
