"""Flags do piloto de IA no fluxo clínico, desabilitadas por padrão."""

from alembic import op
import sqlalchemy as sa

revision = "ai20261001a"
down_revision = "gc20260930a"
branch_labels = None
depends_on = None

FLAGS = {
    "ai_evolution_dictation": "Evolução por ditado com rascunho de IA",
    "ai_assessment_goals": "Sugestão de metas a partir da avaliação",
}


def upgrade() -> None:
    for key, description in FLAGS.items():
        op.execute(sa.text(
            "INSERT INTO feature_flags (key, description, enabled_global) "
            "VALUES (:key, :description, false) ON CONFLICT (key) DO NOTHING"
        ).bindparams(key=key, description=description))


def downgrade() -> None:
    for key in FLAGS:
        op.execute(sa.text("DELETE FROM feature_flags WHERE key = :key").bindparams(key=key))
