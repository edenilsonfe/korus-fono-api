"""Repair billing document columns after the earlier h0 revision was applied.

Revision ID: i0j1k2l3m4n5
Revises: h0i1j2k3l4m5
Create Date: 2026-08-18
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "i0j1k2l3m4n5"
down_revision: Union[str, None] = "h0i1j2k3l4m5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    professional_columns = _column_names("professionals")
    had_legacy_document = "billing_document" in professional_columns

    if "billing_cnpj" not in professional_columns:
        op.add_column(
            "professionals",
            sa.Column("billing_cnpj", sa.String(length=14), server_default="", nullable=False),
        )
    if "billing_document_type" not in professional_columns:
        op.add_column(
            "professionals",
            sa.Column(
                "billing_document_type",
                sa.String(length=4),
                server_default="cpf",
                nullable=False,
            ),
        )

    subscription_columns = _column_names("subscriptions")
    if "billing_document" not in subscription_columns:
        op.add_column(
            "subscriptions",
            sa.Column("billing_document", sa.String(length=14), server_default="", nullable=False),
        )

    if had_legacy_document:
        op.execute(
            "UPDATE professionals "
            "SET billing_cnpj = regexp_replace(billing_document, '\\D', '', 'g'), "
            "billing_document_type = 'cnpj' "
            "WHERE char_length(regexp_replace(billing_document, '\\D', '', 'g')) = 14"
        )
        op.execute(
            "UPDATE professionals "
            "SET cpf = regexp_replace(billing_document, '\\D', '', 'g'), "
            "billing_document_type = 'cpf' "
            "WHERE char_length(regexp_replace(billing_document, '\\D', '', 'g')) = 11"
        )

    op.execute(
        "UPDATE subscriptions AS s "
        "SET billing_document = CASE "
        "WHEN p.billing_document_type = 'cnpj' AND p.billing_cnpj <> '' THEN p.billing_cnpj "
        "ELSE p.cpf END "
        "FROM professionals AS p "
        "WHERE s.professional_id = p.id "
        "AND COALESCE(s.billing_document, '') = ''"
    )

    if had_legacy_document:
        op.drop_column("professionals", "billing_document")


def downgrade() -> None:
    # Forward-only repair: h0 owns the final columns. Dropping them here would
    # corrupt databases where h0 had already created the correct final schema.
    pass
