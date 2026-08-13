"""default financial categories

Revision ID: f3e4d5c6b7a8
Revises: f2e3d4c5b6a7
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f3e4d5c6b7a8"
down_revision: Union[str, None] = "f2e3d4c5b6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO financial_categories
            (id, professional_id, name, kind, active, created_at, updated_at)
        SELECT
            md5(p.id::text || ':' || defaults.kind || ':' || defaults.name)::uuid,
            p.id,
            defaults.name,
            defaults.kind,
            true,
            now(),
            now()
        FROM professionals AS p
        CROSS JOIN (
            VALUES
                ('income', 'Atendimentos'),
                ('income', 'Avaliações'),
                ('income', 'Pacotes'),
                ('income', 'Taxas de cancelamento'),
                ('income', 'Outras receitas'),
                ('expense', 'Aluguel e estrutura'),
                ('expense', 'Materiais e insumos'),
                ('expense', 'Serviços de terceiros'),
                ('expense', 'Impostos e taxas'),
                ('expense', 'Outras despesas')
        ) AS defaults(kind, name)
        WHERE NOT EXISTS (
            SELECT 1
            FROM financial_categories AS current
            WHERE current.professional_id = p.id
              AND current.kind = defaults.kind
              AND lower(current.name) = lower(defaults.name)
        )
        ON CONFLICT (professional_id, kind, name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM financial_categories AS category
        USING professionals AS p,
              (
                  VALUES
                      ('income', 'Atendimentos'),
                      ('income', 'Avaliações'),
                      ('income', 'Pacotes'),
                      ('income', 'Taxas de cancelamento'),
                      ('income', 'Outras receitas'),
                      ('expense', 'Aluguel e estrutura'),
                      ('expense', 'Materiais e insumos'),
                      ('expense', 'Serviços de terceiros'),
                      ('expense', 'Impostos e taxas'),
                      ('expense', 'Outras despesas')
              ) AS defaults(kind, name)
        WHERE category.professional_id = p.id
          AND category.kind = defaults.kind
          AND category.name = defaults.name
          AND category.id = md5(p.id::text || ':' || defaults.kind || ':' || defaults.name)::uuid
        """
    )
