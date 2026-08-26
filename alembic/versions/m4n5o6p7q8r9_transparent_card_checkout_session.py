"""Add stable local sessions for transparent card checkout.

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m4n5o6p7q8r9"
down_revision: Union[str, None] = "l3m4n5o6p7q8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("checkout_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("checkout_charge_cents", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_subscriptions_checkout_session_id",
        "subscriptions",
        ["checkout_session_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_checkout_session_id", table_name="subscriptions")
    op.drop_column("subscriptions", "checkout_charge_cents")
    op.drop_column("subscriptions", "checkout_session_id")
