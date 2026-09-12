"""f14_family_portal_publications

Revision ID: 1f2db0ef179a
Revises: 5116e40d2738
Create Date: 2026-09-11 21:07:58.609066

Nota de revisão (curadoria obrigatória — plano §6.3): o autogenerate também
capturou o drift pré-existente do banco de dev (affiliates, app_notifications,
churn de FK em appointments, índices de battery_*, constraints de
financial_profiles/google_calendar_connections/notification_settings) — removido
de propósito; segue como dívida para um ciclo de limpeza próprio. Esta revisão
contém apenas a onda F14/M2: as três tabelas do conteúdo editorial do portal da
família (family_portal_items / family_portal_item_revisions /
family_portal_item_audiences) com CHECKs fechados por kind/status/fonte,
ponteiro de publicação sem FK circular (par item_id+published_version protegido
pelo UNIQUE da revisão; órfão falha fechado no serviço) e o índice de leitura
(público) portal+kind+status. Nenhum backfill: nada é copiado de evoluções,
metas, recursos, entregas ou destinatários; nenhuma licença é inferida.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f2db0ef179a'
down_revision: Union[str, Sequence[str], None] = '5116e40d2738'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('family_portal_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('portal_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('published_version', sa.Integer(), nullable=True),
    sa.Column('draft_content', sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('draft_recipient_ids', sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('draft_source_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('evolution_id', sa.UUID(), nullable=True),
    sa.Column('goal_id', sa.UUID(), nullable=True),
    sa.Column('resource_id', sa.UUID(), nullable=True),
    sa.Column('delivery_id', sa.UUID(), nullable=True),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'session_summary' AND session_id IS NOT NULL AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NULL) OR (kind = 'goal' AND session_id IS NULL AND evolution_id IS NULL AND goal_id IS NOT NULL AND resource_id IS NULL AND delivery_id IS NULL) OR (kind = 'notice' AND session_id IS NULL AND evolution_id IS NULL AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NULL) OR (kind = 'material' AND session_id IS NULL AND evolution_id IS NULL AND goal_id IS NULL AND resource_id IS NOT NULL AND delivery_id IS NULL) OR (kind = 'report' AND session_id IS NULL AND evolution_id IS NULL AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NOT NULL)", name='ck_family_portal_items_source'),
    sa.CheckConstraint("kind IN ('session_summary', 'goal', 'notice', 'material', 'report')", name='ck_family_portal_items_kind'),
    sa.CheckConstraint("status <> 'published' OR published_version IS NOT NULL", name='ck_family_portal_items_published_pointer'),
    sa.CheckConstraint("status IN ('draft', 'published', 'withdrawn')", name='ck_family_portal_items_status'),
    sa.CheckConstraint('published_version IS NULL OR published_version <= version', name='ck_family_portal_items_published_not_future'),
    sa.CheckConstraint('published_version IS NULL OR published_version >= 1', name='ck_family_portal_items_published_version'),
    sa.CheckConstraint('version >= 1', name='ck_family_portal_items_version'),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['delivery_id'], ['report_deliveries.id'], ),
    sa.ForeignKeyConstraint(['evolution_id'], ['evolutions.id'], ),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ),
    sa.ForeignKeyConstraint(['portal_id'], ['family_portals.id'], ),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_family_portal_items_delivery', 'family_portal_items', ['delivery_id'], unique=False)
    op.create_index('ix_family_portal_items_evolution', 'family_portal_items', ['evolution_id'], unique=False)
    op.create_index('ix_family_portal_items_goal', 'family_portal_items', ['goal_id'], unique=False)
    op.create_index(op.f('ix_family_portal_items_portal_id'), 'family_portal_items', ['portal_id'], unique=False)
    op.create_index('ix_family_portal_items_portal_kind_status', 'family_portal_items', ['portal_id', 'kind', 'status', 'published_at', 'id'], unique=False)
    op.create_index('ix_family_portal_items_resource', 'family_portal_items', ['resource_id'], unique=False)
    op.create_index('ix_family_portal_items_session', 'family_portal_items', ['session_id'], unique=False)
    op.create_table('family_portal_item_audiences',
    sa.Column('item_id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['family_portal_items.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['family_portal_recipients.id'], ),
    sa.PrimaryKeyConstraint('item_id', 'recipient_id')
    )
    op.create_index('ix_family_portal_item_audiences_recipient', 'family_portal_item_audiences', ['recipient_id', 'item_id'], unique=False)
    op.create_table('family_portal_item_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('item_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('recipient_ids', sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('source_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('source_metadata', sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('published_by_professional_id', sa.UUID(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('version >= 1', name='ck_family_portal_item_revision_version'),
    sa.ForeignKeyConstraint(['item_id'], ['family_portal_items.id'], ),
    sa.ForeignKeyConstraint(['published_by_professional_id'], ['professionals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id', 'version', name='uq_family_portal_item_revision')
    )
    op.create_index(op.f('ix_family_portal_item_revisions_item_id'), 'family_portal_item_revisions', ['item_id'], unique=False)
    op.create_index('ix_family_portal_item_revisions_item_published', 'family_portal_item_revisions', ['item_id', 'published_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_family_portal_item_revisions_item_published', table_name='family_portal_item_revisions')
    op.drop_index(op.f('ix_family_portal_item_revisions_item_id'), table_name='family_portal_item_revisions')
    op.drop_table('family_portal_item_revisions')
    op.drop_index('ix_family_portal_item_audiences_recipient', table_name='family_portal_item_audiences')
    op.drop_table('family_portal_item_audiences')
    op.drop_index('ix_family_portal_items_session', table_name='family_portal_items')
    op.drop_index('ix_family_portal_items_resource', table_name='family_portal_items')
    op.drop_index('ix_family_portal_items_portal_kind_status', table_name='family_portal_items')
    op.drop_index(op.f('ix_family_portal_items_portal_id'), table_name='family_portal_items')
    op.drop_index('ix_family_portal_items_goal', table_name='family_portal_items')
    op.drop_index('ix_family_portal_items_evolution', table_name='family_portal_items')
    op.drop_index('ix_family_portal_items_delivery', table_name='family_portal_items')
    op.drop_table('family_portal_items')
