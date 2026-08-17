"""Link appointments to financial services and snapshot the agreed price.

Revision ID: f6e7d8c9b0a1
Revises: f5e6d7c8b9a0
Create Date: 2026-08-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6e7d8c9b0a1"
down_revision: Union[str, None] = "f5e6d7c8b9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("service_name_snapshot", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("service_price_cents", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_appointments_service_id_financial_services",
        "appointments",
        "financial_services",
        ["service_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_appointments_service_id",
        "appointments",
        ["service_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_appointments_service_id", table_name="appointments")
    op.drop_constraint(
        "fk_appointments_service_id_financial_services",
        "appointments",
        type_="foreignkey",
    )
    op.drop_column("appointments", "service_price_cents")
    op.drop_column("appointments", "service_name_snapshot")
    op.drop_column("appointments", "service_id")
