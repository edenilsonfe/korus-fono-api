"""f3_report_composition_versions

Revision ID: 4147477648e9
Revises: 55f75dbb1265
Create Date: 2026-09-11 05:43:22.539428

Nota de revisão (curadoria obrigatória — plano §6.3): o autogenerate também
capturou drift pré-existente do banco local (constraints/índices de affiliates,
app_notifications, churn de FK em appointments, renomeações de índices de
battery_*, unique de financial_profiles/google_calendar_connections/
notification_settings). Todo esse drift foi REMOVIDO desta revisão e segue
pendente de um ciclo próprio de reconciliação — não é trabalho da F3.

Contém apenas: tabela `ai_report_compositions` (+índices), colunas `version`
em `ai_reports` (server_default '1' — backfill das linhas atuais) e
`ai_report_revisions` (nullable — revisões legadas ficam null), e
`report_deliveries.document_snapshot` (nullable — entregas legadas ficam null,
sem fabricar texto de entrega antiga).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4147477648e9'
down_revision: Union[str, Sequence[str], None] = '55f75dbb1265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ai_report_compositions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('professional_id', sa.UUID(), nullable=False),
    sa.Column('selection', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_hashes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('context_hash', sa.String(length=64), nullable=False),
    sa.Column('template_version', sa.String(length=32), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('supersedes_report_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['report_id'], ['ai_reports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['supersedes_report_id'], ['ai_reports.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ai_report_compositions_professional_id'), 'ai_report_compositions', ['professional_id'], unique=False)
    op.create_index(op.f('ix_ai_report_compositions_report_id'), 'ai_report_compositions', ['report_id'], unique=True)
    op.add_column('ai_report_revisions', sa.Column('version', sa.Integer(), nullable=True))
    op.add_column('ai_reports', sa.Column('version', sa.Integer(), server_default='1', nullable=False))
    op.add_column('report_deliveries', sa.Column('document_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('report_deliveries', 'document_snapshot')
    op.drop_column('ai_reports', 'version')
    op.drop_column('ai_report_revisions', 'version')
    op.drop_index(op.f('ix_ai_report_compositions_report_id'), table_name='ai_report_compositions')
    op.drop_index(op.f('ix_ai_report_compositions_professional_id'), table_name='ai_report_compositions')
    op.drop_table('ai_report_compositions')
