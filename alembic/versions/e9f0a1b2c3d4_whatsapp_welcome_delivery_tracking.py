"""whatsapp welcome delivery tracking

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notification_message_logs",
        sa.Column("deduplication_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_notification_message_logs_deduplication_key",
        "notification_message_logs",
        ["deduplication_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_message_logs_deduplication_key",
        table_name="notification_message_logs",
    )
    op.drop_column("notification_message_logs", "deduplication_key")
