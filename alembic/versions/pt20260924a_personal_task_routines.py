"""Add appointment context, checklist, recurrence and due reminders to personal tasks.

Revision ID: pt20260924a
Revises: cdf4e68e12ab
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pt20260924a"
down_revision = "cdf4e68e12ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_tasks", sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("personal_tasks", sa.Column("repeat_rule", sa.String(length=16), server_default="none", nullable=False))
    op.add_column("personal_tasks", sa.Column("repeat_day", sa.Integer(), nullable=True))
    op.add_column("personal_tasks", sa.Column("remind_before_days", sa.Integer(), nullable=True))
    op.add_column("personal_tasks", sa.Column("checklist", sa.JSON(), server_default="[]", nullable=False))
    op.create_foreign_key("fk_personal_tasks_appointment_id", "personal_tasks", "appointments", ["appointment_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint("ck_personal_tasks_repeat_rule", "personal_tasks", "repeat_rule IN ('none', 'daily', 'weekly', 'monthly')")
    op.create_check_constraint("ck_personal_tasks_remind_before_days", "personal_tasks", "remind_before_days IS NULL OR remind_before_days BETWEEN 0 AND 30")
    op.create_check_constraint("ck_personal_tasks_repeat_day", "personal_tasks", "repeat_day IS NULL OR repeat_day BETWEEN 1 AND 31")


def downgrade() -> None:
    op.drop_constraint("ck_personal_tasks_remind_before_days", "personal_tasks", type_="check")
    op.drop_constraint("ck_personal_tasks_repeat_day", "personal_tasks", type_="check")
    op.drop_constraint("ck_personal_tasks_repeat_rule", "personal_tasks", type_="check")
    op.drop_constraint("fk_personal_tasks_appointment_id", "personal_tasks", type_="foreignkey")
    op.drop_column("personal_tasks", "checklist")
    op.drop_column("personal_tasks", "remind_before_days")
    op.drop_column("personal_tasks", "repeat_rule")
    op.drop_column("personal_tasks", "repeat_day")
    op.drop_column("personal_tasks", "appointment_id")
