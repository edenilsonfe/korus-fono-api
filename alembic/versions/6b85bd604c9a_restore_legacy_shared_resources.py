"""restore_legacy_shared_resources

Revision ID: 6b85bd604c9a
Revises: eaa1bbe3701c
Create Date: 2026-09-14 14:31:52.292829

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6b85bd604c9a'
down_revision: Union[str, Sequence[str], None] = 'eaa1bbe3701c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Recupera materiais pessoais explicitamente compartilhados antes de F17.

    Hash ausente identifica o conteúdo antigo; uploads novos gravam hash.
    Mantém autoria/propriedade e não toca privados, arquivados ou licenciados.
    Não cria licença nem autoriza entrega familiar ou novos vínculos clínicos.
    """
    op.execute(sa.text("""
        UPDATE resources
        SET publication_status = 'published'
        WHERE owner_professional_id IS NOT NULL
          AND shared_with_platform IS TRUE
          AND publication_status = 'draft'
          AND archived_at IS NULL
          AND content_sha256 IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM resource_licenses
              WHERE resource_licenses.resource_id = resources.id
          )
    """))


def downgrade() -> None:
    """Não oculta novamente os compartilhamentos recuperados em rollback."""
    pass
