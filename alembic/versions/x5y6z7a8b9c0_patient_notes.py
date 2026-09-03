"""Add optional patient notes.

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
"""

import sqlalchemy as sa

from alembic import op

revision = "x5y6z7a8b9c0"
down_revision = "w4x5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "notes")
