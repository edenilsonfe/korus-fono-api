"""Add the opt-in preference for in-app birthday reminders.

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
"""

import sqlalchemy as sa

from alembic import op

revision = "v3w4x5y6z7a8"
down_revision = "u2v3w4x5y6z7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_settings",
        sa.Column(
            "birthday_in_app_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_settings", "birthday_in_app_enabled")
