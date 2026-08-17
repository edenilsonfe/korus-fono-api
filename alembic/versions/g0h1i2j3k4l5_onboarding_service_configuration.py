"""Add service configuration to onboarding version 2.

Revision ID: g0h1i2j3k4l5
Revises: f6e7d8c9b0a1
Create Date: 2026-08-17
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "g0h1i2j3k4l5"
down_revision: Union[str, None] = "f6e7d8c9b0a1"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.alter_column(
        "professionals",
        "onboarding_version",
        existing_type=sa.Integer(),
        server_default=sa.text("2"),
        existing_nullable=False,
    )
    op.execute("UPDATE professionals SET onboarding_version = 2 WHERE onboarding_version < 2")


def downgrade() -> None:
    op.execute("UPDATE professionals SET onboarding_version = 1 WHERE onboarding_version = 2")
    op.alter_column(
        "professionals",
        "onboarding_version",
        existing_type=sa.Integer(),
        server_default=sa.text("1"),
        existing_nullable=False,
    )
