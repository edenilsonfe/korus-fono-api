import uuid
from datetime import date, time

from sqlalchemy import Date, ForeignKey, Integer, String, Time
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_uuid


class Appointment(Base, TimestampMixin):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("financial_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_name_snapshot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    service_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pendente")
    appointment_type: Mapped[str] = mapped_column(String(32), nullable=False, default="avulso")
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id"), nullable=True, index=True
    )
    frequency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    weekdays: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    weekday_slots: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

    patient: Mapped["Patient"] = relationship(back_populates="appointments")  # noqa: F821
