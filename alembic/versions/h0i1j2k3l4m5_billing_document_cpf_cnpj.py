"""Store CPF and CNPJ billing choices and charge document snapshot.

Revision ID: h0i1j2k3l4m5
Revises: g0h1i2j3k4l5
Create Date: 2026-08-18
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "h0i1j2k3l4m5"
down_revision: Union[str, None] = "g0h1i2j3k4l5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column("billing_cnpj", sa.String(length=14), server_default="", nullable=False),
    )
    op.add_column(
        "professionals",
        sa.Column(
            "billing_document_type",
            sa.String(length=4),
            server_default="cpf",
            nullable=False,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("billing_document", sa.String(length=14), server_default="", nullable=False),
    )
    op.execute(
        "UPDATE subscriptions AS s SET billing_document = p.cpf "
        "FROM professionals AS p "
        "WHERE s.professional_id = p.id AND p.cpf IS NOT NULL AND p.cpf <> ''"
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_document")
    op.drop_column("professionals", "billing_document_type")
    op.drop_column("professionals", "billing_cnpj")
