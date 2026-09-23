"""Flags para o piloto da jornada clínica, desabilitadas por padrão."""

from alembic import op
import sqlalchemy as sa

revision = "cw20260922a"
down_revision = "in20260922a"
branch_labels = None
depends_on = None

FLAGS = {
    "functional_feedback": "Retorno funcional da família nas tarefas de casa",
    "clinical_reviews": "Revisão terapêutica periódica",
    "structured_discharge": "Alta com plano de continuidade",
    "patient_intake": "Pré-atendimento digital por convite",
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
