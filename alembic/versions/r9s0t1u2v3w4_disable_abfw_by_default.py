"""Disable ABFW in the protocol catalog by default.

Revision ID: r9s0t1u2v3w4
Revises: q8r9s0t1u2v3
"""

from alembic import op
import sqlalchemy as sa


revision = "r9s0t1u2v3w4"
down_revision = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE protocol_catalog
            SET is_active = false, updated_at = now()
            WHERE id = 'abfw' AND is_active IS DISTINCT FROM false
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE protocol_catalog
            SET is_active = true, updated_at = now()
            WHERE id = 'abfw' AND is_active IS DISTINCT FROM true
            """
        )
    )
