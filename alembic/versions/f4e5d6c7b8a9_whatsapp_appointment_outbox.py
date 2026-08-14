"""use notification logs as a durable WhatsApp appointment outbox

Revision ID: f4e5d6c7b8a9
Revises: f3e4d5c6b7a8
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f4e5d6c7b8a9"
down_revision: Union[str, None] = "f3e4d5c6b7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New appointment events use the existing unique deduplication_key. The old
    # slot-only constraint prevented a legitimate later reschedule back to a
    # previously used time from creating a new durable event.
    op.drop_constraint(
        "uq_notification_message_idempotency",
        "notification_message_logs",
        type_="unique",
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_notification_message_idempotency",
        "notification_message_logs",
        [
            "appointment_id",
            "notification_type",
            "channel",
            "scheduled_date",
            "scheduled_time",
            "is_test",
        ],
    )
