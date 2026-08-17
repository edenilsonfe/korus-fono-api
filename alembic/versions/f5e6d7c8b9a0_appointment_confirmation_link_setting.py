"""Add opt-in setting for appointment confirmation links.

Revision ID: f5e6d7c8b9a0
Revises: f4e5d6c7b8a9
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5e6d7c8b9a0"
down_revision: Union[str, None] = "f4e5d6c7b8a9"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "notification_settings",
        sa.Column(
            "appointment_confirmation_link_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "notification_settings", "appointment_confirmation_link_enabled"
    )
