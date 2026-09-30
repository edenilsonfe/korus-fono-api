"""Google Calendar: browser-bound OAuth and versioned sync claims.

Revision ID: gc20260930a
Revises: pt20260925a
Create Date: 2026-09-30 01:30:52.450280

"""
from alembic import op
import sqlalchemy as sa

revision = "gc20260930a"
down_revision = "pt20260925a"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "google_calendar_oauth_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f('ix_google_calendar_oauth_requests_expires_at'), 'google_calendar_oauth_requests', ['expires_at'], unique=False)
    op.create_index(op.f('ix_google_calendar_oauth_requests_professional_id'), 'google_calendar_oauth_requests', ['professional_id'], unique=False)
    op.add_column('google_calendar_sync_records', sa.Column('sync_version', sa.Integer(), server_default='1', nullable=False))
    op.add_column('google_calendar_sync_records', sa.Column('processing_token', sa.UUID(), nullable=True))
    op.add_column('google_calendar_sync_records', sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column('google_calendar_sync_records', 'processing_started_at')
    op.drop_column('google_calendar_sync_records', 'processing_token')
    op.drop_column('google_calendar_sync_records', 'sync_version')
    op.drop_index(op.f('ix_google_calendar_oauth_requests_professional_id'), table_name='google_calendar_oauth_requests')
    op.drop_index(op.f('ix_google_calendar_oauth_requests_expires_at'), table_name='google_calendar_oauth_requests')
    op.drop_table('google_calendar_oauth_requests')
