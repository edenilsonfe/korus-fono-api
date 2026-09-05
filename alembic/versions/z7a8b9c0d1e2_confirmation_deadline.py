"""Optional previous-day deadline for public appointment responses.

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
"""

from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_settings",
        sa.Column("appointment_confirmation_deadline_time", sa.String(5), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_settings", "appointment_confirmation_deadline_time")
