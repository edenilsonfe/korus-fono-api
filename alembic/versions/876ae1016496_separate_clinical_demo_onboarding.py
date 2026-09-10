"""Separate clinical demonstration from real-use onboarding.

Revision ID: 876ae1016496
Revises: d5e6f7a8b9c0
"""
from alembic import op
import sqlalchemy as sa

revision = "876ae1016496"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reviewed autogenerate: discard SQLite UUID reflection differences.
    op.add_column("professionals", sa.Column("onboarding_reviewed_demo_report_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("professionals", sa.Column("onboarding_skipped_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("professionals", "onboarding_version", existing_type=sa.Integer(), server_default="3", existing_nullable=False)
    op.execute("UPDATE professionals SET onboarding_version = 3 WHERE onboarding_version < 3")


def downgrade() -> None:
    op.drop_column("professionals", "onboarding_skipped_at")
    op.drop_column("professionals", "onboarding_reviewed_demo_report_at")
    op.alter_column("professionals", "onboarding_version", existing_type=sa.Integer(), server_default="2", existing_nullable=False)
    op.execute("UPDATE professionals SET onboarding_version = 2 WHERE onboarding_version = 3")
