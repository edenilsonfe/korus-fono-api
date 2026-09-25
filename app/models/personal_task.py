import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class PersonalTask(Base, TimestampMixin):
    __tablename__ = "personal_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('todo', 'doing', 'done')", name="ck_personal_tasks_status"),
        CheckConstraint("repeat_rule IN ('none', 'daily', 'weekly', 'monthly')", name="ck_personal_tasks_repeat_rule"),
        CheckConstraint("remind_before_days IS NULL OR remind_before_days BETWEEN 0 AND 30", name="ck_personal_tasks_remind_before_days"),
        CheckConstraint("repeat_day IS NULL OR repeat_day BETWEEN 1 AND 31", name="ck_personal_tasks_repeat_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    repeat_rule: Mapped[str] = mapped_column(String(16), nullable=False, default="none", server_default="none")
    repeat_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remind_before_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checklist: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    column_key: Mapped[str] = mapped_column(String(36), nullable=False, default="todo", server_default="todo")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="todo", server_default="todo")
