"""Allow personal task columns and preserve existing card positions.

Revision ID: pt20260925a
Revises: pt20260924a
"""

from alembic import op
import sqlalchemy as sa


revision = "pt20260925a"
down_revision = "pt20260924a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("professionals", sa.Column("personal_task_columns", sa.JSON(), nullable=True))
    op.add_column("personal_tasks", sa.Column("column_key", sa.String(length=36), server_default="todo", nullable=False))
    op.execute("UPDATE personal_tasks SET column_key = status")


def downgrade() -> None:
    op.drop_column("personal_tasks", "column_key")
    op.drop_column("professionals", "personal_task_columns")
