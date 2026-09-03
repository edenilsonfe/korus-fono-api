"""Persist the payment method selected for a subscription.

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
"""

from alembic import op
import sqlalchemy as sa


revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("payment_method", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_subscriptions_payment_method",
        "subscriptions",
        "payment_method IS NULL OR payment_method IN ('pix', 'credit_card')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_subscriptions_payment_method",
        "subscriptions",
        type_="check",
    )
    op.drop_column("subscriptions", "payment_method")
