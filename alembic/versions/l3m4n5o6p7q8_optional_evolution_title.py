"""Make the evolution title optional.

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l3m4n5o6p7q8"
down_revision: Union[str, None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "evolutions",
        "title",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE evolutions SET title = 'Evolução registrada' WHERE title IS NULL")
    op.alter_column(
        "evolutions",
        "title",
        existing_type=sa.String(length=255),
        nullable=False,
    )
