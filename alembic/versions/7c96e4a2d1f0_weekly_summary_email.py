"""Add weekly summary email preferences and delivery audit.

Revision ID: 7c96e4a2d1f0
Revises: 6b85bd604c9a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "7c96e4a2d1f0"
down_revision = "6b85bd604c9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_settings",
        sa.Column(
            "weekly_summary_email_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "notification_settings",
        sa.Column("weekly_summary_email_opted_in_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "notification_settings",
        sa.Column("weekly_summary_email_opted_out_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "notification_settings",
        sa.Column("weekly_summary_email_preference_source", sa.String(length=32)),
    )
    op.add_column(
        "notification_settings",
        sa.Column("weekly_summary_email_suppressed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "notification_settings",
        sa.Column("weekly_summary_email_suppression_reason", sa.String(length=32)),
    )

    op.create_table(
        "weekly_summary_email_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_message_id", sa.String(length=255)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("skip_reason", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "professional_id",
            "week_start",
            name="uq_weekly_summary_professional_week",
        ),
    )
    op.create_index(
        "ix_weekly_summary_email_deliveries_professional_id",
        "weekly_summary_email_deliveries",
        ["professional_id"],
    )
    op.create_index(
        "ix_weekly_summary_email_deliveries_week_start",
        "weekly_summary_email_deliveries",
        ["week_start"],
    )
    op.create_index(
        "ix_weekly_summary_email_deliveries_status",
        "weekly_summary_email_deliveries",
        ["status"],
    )
    op.create_index(
        "ix_weekly_summary_email_deliveries_provider_message_id",
        "weekly_summary_email_deliveries",
        ["provider_message_id"],
    )


def downgrade() -> None:
    op.drop_table("weekly_summary_email_deliveries")
    op.drop_column("notification_settings", "weekly_summary_email_suppression_reason")
    op.drop_column("notification_settings", "weekly_summary_email_suppressed_at")
    op.drop_column("notification_settings", "weekly_summary_email_preference_source")
    op.drop_column("notification_settings", "weekly_summary_email_opted_out_at")
    op.drop_column("notification_settings", "weekly_summary_email_opted_in_at")
    op.drop_column("notification_settings", "weekly_summary_email_enabled")
