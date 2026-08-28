"""Add Google Calendar OAuth connection and durable event sync.

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "q8r9s0t1u2v3"
down_revision = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendar_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=False),
        sa.Column("include_patient_name", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("professional_id"),
    )
    op.create_index("ix_google_calendar_connections_professional_id", "google_calendar_connections", ["professional_id"], unique=True)
    op.create_table(
        "google_calendar_sync_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("google_event_id", sa.String(length=255), nullable=True),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("event_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("appointment_id", name="uq_google_calendar_sync_appointment"),
    )
    op.create_index("ix_google_calendar_sync_records_professional_id", "google_calendar_sync_records", ["professional_id"])
    op.create_index("ix_google_calendar_sync_records_appointment_id", "google_calendar_sync_records", ["appointment_id"])
    op.create_index("ix_google_calendar_sync_records_status", "google_calendar_sync_records", ["status"])


def downgrade() -> None:
    op.drop_index("ix_google_calendar_sync_records_status", table_name="google_calendar_sync_records")
    op.drop_index("ix_google_calendar_sync_records_appointment_id", table_name="google_calendar_sync_records")
    op.drop_index("ix_google_calendar_sync_records_professional_id", table_name="google_calendar_sync_records")
    op.drop_table("google_calendar_sync_records")
    op.drop_index("ix_google_calendar_connections_professional_id", table_name="google_calendar_connections")
    op.drop_table("google_calendar_connections")
