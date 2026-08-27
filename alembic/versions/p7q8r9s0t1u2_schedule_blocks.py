"""Add professional schedule blocks.

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "p7q8r9s0t1u2"
down_revision = "o6p7q8r9s0t1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="ck_schedule_blocks_date_range"),
        sa.CheckConstraint(
            "(start_time IS NULL AND end_time IS NULL) OR "
            "(start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)",
            name="ck_schedule_blocks_time_range",
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"],
            ["professionals.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedule_blocks_professional_id", "schedule_blocks", ["professional_id"])
    op.create_index("ix_schedule_blocks_start_date", "schedule_blocks", ["start_date"])
    op.create_index("ix_schedule_blocks_end_date", "schedule_blocks", ["end_date"])


def downgrade() -> None:
    op.drop_index("ix_schedule_blocks_end_date", table_name="schedule_blocks")
    op.drop_index("ix_schedule_blocks_start_date", table_name="schedule_blocks")
    op.drop_index("ix_schedule_blocks_professional_id", table_name="schedule_blocks")
    op.drop_table("schedule_blocks")
