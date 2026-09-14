"""restore_legacy_global_catalog

Revision ID: eaa1bbe3701c
Revises: 1f2db0ef179a
Create Date: 2026-09-14 14:21:02.676820

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eaa1bbe3701c'
down_revision: Union[str, Sequence[str], None] = '1f2db0ef179a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Restaura apenas o catálogo global que F17 converteu em rascunho.

    Antes de F17, todo material global era visível. Os uploads e seeds novos
    já gravam content_sha256; NULL identifica o conteúdo anterior. Não cria
    licença, não libera entrega familiar e não toca materiais já revisados.
    Preserva updated_at, arquivos e contadores.
    """
    op.execute(sa.text("""
        UPDATE resources
        SET publication_status = 'published'
        WHERE owner_professional_id IS NULL
          AND publication_status = 'draft'
          AND archived_at IS NULL
          AND content_sha256 IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM resource_licenses
              WHERE resource_licenses.resource_id = resources.id
          )
    """))


def downgrade() -> None:
    """Não oculta novamente um catálogo recuperado em um rollback de código."""
    pass
