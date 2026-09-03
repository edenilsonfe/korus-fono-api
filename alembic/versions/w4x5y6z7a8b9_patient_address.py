"""Add optional patient address.

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
"""

import sqlalchemy as sa

from alembic import op

revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("address", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "address")
