"""platform whatsapp welcome message

Revision ID: c7d8e9f0a1b2
Revises: f7d93c8db339
Create Date: 2026-08-05 18:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, Sequence[str], None] = 'f7d93c8db339'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Custom welcome message edited in the admin panel (NULL = default text).
    op.add_column(
        'platform_whatsapp_connections',
        sa.Column('welcome_message', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('platform_whatsapp_connections', 'welcome_message')
