"""trial email campaigns

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trial_email_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("expires_within_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("eligible_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("suppressed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["professionals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trial_email_campaigns_actor_id", "trial_email_campaigns", ["actor_id"])
    op.create_index("ix_trial_email_campaigns_audience", "trial_email_campaigns", ["audience"])
    op.create_index("ix_trial_email_campaigns_status", "trial_email_campaigns", ["status"])

    op.create_table(
        "trial_email_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["trial_email_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "professional_id", name="uq_trial_email_delivery_recipient"),
    )
    op.create_index("ix_trial_email_deliveries_campaign_id", "trial_email_deliveries", ["campaign_id"])
    op.create_index("ix_trial_email_deliveries_professional_id", "trial_email_deliveries", ["professional_id"])
    op.create_index("ix_trial_email_deliveries_status", "trial_email_deliveries", ["status"])


def downgrade() -> None:
    op.drop_index("ix_trial_email_deliveries_status", table_name="trial_email_deliveries")
    op.drop_index("ix_trial_email_deliveries_professional_id", table_name="trial_email_deliveries")
    op.drop_index("ix_trial_email_deliveries_campaign_id", table_name="trial_email_deliveries")
    op.drop_table("trial_email_deliveries")
    op.drop_index("ix_trial_email_campaigns_status", table_name="trial_email_campaigns")
    op.drop_index("ix_trial_email_campaigns_audience", table_name="trial_email_campaigns")
    op.drop_index("ix_trial_email_campaigns_actor_id", table_name="trial_email_campaigns")
    op.drop_table("trial_email_campaigns")
