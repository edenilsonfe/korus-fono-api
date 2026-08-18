"""Add payer address fields to the professional billing profile.

Revision ID: k2l3m4n5o6p7
Revises: i0j1k2l3m4n5
Create Date: 2026-08-18
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "k2l3m4n5o6p7"
down_revision: Union[str, None] = "i0j1k2l3m4n5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column("billing_address", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column(
        "professionals",
        sa.Column(
            "billing_address_number", sa.String(length=30), server_default="", nullable=False
        ),
    )
    op.add_column(
        "professionals",
        sa.Column(
            "billing_address_complement",
            sa.String(length=100),
            server_default="",
            nullable=False,
        ),
    )
    op.add_column(
        "professionals",
        sa.Column("billing_province", sa.String(length=100), server_default="", nullable=False),
    )
    op.add_column(
        "professionals",
        sa.Column("billing_postal_code", sa.String(length=8), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("professionals", "billing_postal_code")
    op.drop_column("professionals", "billing_province")
    op.drop_column("professionals", "billing_address_complement")
    op.drop_column("professionals", "billing_address_number")
    op.drop_column("professionals", "billing_address")
