"""default financial payment methods

Revision ID: f2e3d4c5b6a7
Revises: f1e2d3c4b5a6
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f2e3d4c5b6a7"
down_revision: Union[str, None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO financial_payment_methods
            (id, professional_id, name, active, created_at, updated_at)
        SELECT
            md5(p.id::text || ':' || defaults.name)::uuid,
            p.id,
            defaults.name,
            true,
            now(),
            now()
        FROM professionals AS p
        CROSS JOIN (
            VALUES
                ('Cartão de crédito'),
                ('Cartão de débito'),
                ('Pix'),
                ('Dinheiro')
        ) AS defaults(name)
        WHERE NOT EXISTS (
            SELECT 1
            FROM financial_payment_methods AS current
            WHERE current.professional_id = p.id
              AND lower(current.name) = lower(defaults.name)
        )
        ON CONFLICT (professional_id, name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM financial_payment_methods AS method
        USING professionals AS p,
              (
                  VALUES
                      ('Cartão de crédito'),
                      ('Cartão de débito'),
                      ('Pix'),
                      ('Dinheiro')
              ) AS defaults(name)
        WHERE method.professional_id = p.id
          AND method.name = defaults.name
          AND method.id = md5(p.id::text || ':' || defaults.name)::uuid
        """
    )
