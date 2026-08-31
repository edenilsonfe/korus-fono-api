"""Disable AMIOFE-E in the protocol catalog by default.

Revision ID: s0t1u2v3w4x5
Revises: r9s0t1u2v3w4
"""

from alembic import op
import sqlalchemy as sa


revision = "s0t1u2v3w4x5"
down_revision = "r9s0t1u2v3w4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE protocol_catalog
            SET is_active = false, updated_at = now()
            WHERE id = 'amiofe' AND is_active IS DISTINCT FROM false
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE protocol_catalog
            SET is_active = true, updated_at = now()
            WHERE id = 'amiofe' AND is_active IS DISTINCT FROM true
            """
        )
    )
