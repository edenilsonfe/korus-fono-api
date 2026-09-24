"""Add a personal task board for each professional.

Revision ID: cdf4e68e12ab
Revises: cp20260923a
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "cdf4e68e12ab"
down_revision = "cp20260923a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="todo", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('todo', 'doing', 'done')", name="ck_personal_tasks_status"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_personal_tasks_professional_id", "personal_tasks", ["professional_id"])


def downgrade() -> None:
    op.drop_index("ix_personal_tasks_professional_id", table_name="personal_tasks")
    op.drop_table("personal_tasks")
