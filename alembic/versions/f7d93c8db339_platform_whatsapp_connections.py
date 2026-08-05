"""platform whatsapp connections

Revision ID: f7d93c8db339
Revises: u0v1w2x3y4z5
Create Date: 2026-08-05 18:35:41.154846

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7d93c8db339'
down_revision: Union[str, Sequence[str], None] = 'u0v1w2x3y4z5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Singleton row: platform's own Evolution connection (welcome messages).
    # Autogenerate also detected unrelated model drift (index renames, FK on
    # appointments, notification_settings unique) — intentionally excluded.
    op.create_table('platform_whatsapp_connections',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('evolution_instance_name', sa.String(length=128), nullable=True),
    sa.Column('encrypted_instance_api_key', sa.Text(), nullable=True),
    sa.Column('display_phone_number', sa.String(length=32), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disconnected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('platform_whatsapp_connections')
