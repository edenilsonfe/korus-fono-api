"""Persist the paid-signup access gate.

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n5o6p7q8r9s0"
down_revision: Union[str, None] = "m4n5o6p7q8r9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column(
            "signup_payment_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("professionals", "signup_payment_required")
