"""f17_resource_licenses_links_cleanup

Revision ID: 73b5bac60d26
Revises: 52d2079a38ae
Create Date: 2026-09-11 08:11:02.533607

Nota de revisão (curadoria obrigatória — plano §6.3): mesmo drift pré-existente
do banco local (affiliates, app_notifications, appointments FK, battery_*,
financial_profiles, google_calendar_connections, notification_settings)
REMOVIDO desta revisão — pendente de ciclo próprio de reconciliação.

Contém: `storage_cleanup_tasks` (fila de limpeza de blobs), `resource_licenses`
+ `resource_license_decisions` (licenças append-only), os três vínculos
(`resource_domain_links`, `goal_resource_links`, `program_resource_links`) e as
colunas editoriais em `resources` (publication_status server_default 'draft' —
backfill conservador: nenhum recurso existente vira publicado; content_sha256;
archived_at). Sem criar licença fictícia para registros existentes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '73b5bac60d26'
down_revision: Union[str, Sequence[str], None] = '52d2079a38ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('storage_cleanup_tasks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('not_before', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_storage_cleanup_tasks_created_by_professional_id'), 'storage_cleanup_tasks', ['created_by_professional_id'], unique=False)
    op.create_index(op.f('ix_storage_cleanup_tasks_status'), 'storage_cleanup_tasks', ['status'], unique=False)
    op.create_index('ix_storage_cleanup_tasks_status_not_before', 'storage_cleanup_tasks', ['status', 'not_before'], unique=False)
    op.create_index(op.f('ix_storage_cleanup_tasks_storage_key'), 'storage_cleanup_tasks', ['storage_key'], unique=False)
    op.create_table('resource_domain_links',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('domain_key', sa.String(length=64), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resource_id', 'domain_key', name='uq_resource_domain_links_resource_key')
    )
    op.create_index(op.f('ix_resource_domain_links_created_by_professional_id'), 'resource_domain_links', ['created_by_professional_id'], unique=False)
    op.create_index(op.f('ix_resource_domain_links_domain_key'), 'resource_domain_links', ['domain_key'], unique=False)
    op.create_index(op.f('ix_resource_domain_links_resource_id'), 'resource_domain_links', ['resource_id'], unique=False)
    op.create_table('resource_licenses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('rights_holder', sa.String(length=255), nullable=False),
    sa.Column('source_reference', sa.String(length=500), nullable=True),
    sa.Column('evidence_reference', sa.String(length=500), nullable=True),
    sa.Column('attribution', sa.String(length=500), nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('allow_professional_distribution', sa.Boolean(), nullable=False),
    sa.Column('allow_family_delivery', sa.Boolean(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('declared_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('declared_by_admin', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['declared_by_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resource_id', 'version', name='uq_resource_licenses_resource_version')
    )
    op.create_index(op.f('ix_resource_licenses_declared_by_professional_id'), 'resource_licenses', ['declared_by_professional_id'], unique=False)
    op.create_index(op.f('ix_resource_licenses_resource_id'), 'resource_licenses', ['resource_id'], unique=False)
    op.create_table('goal_resource_links',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('goal_id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('goal_id', 'resource_id', name='uq_goal_resource_links_goal_resource')
    )
    op.create_index(op.f('ix_goal_resource_links_created_by_professional_id'), 'goal_resource_links', ['created_by_professional_id'], unique=False)
    op.create_index(op.f('ix_goal_resource_links_goal_id'), 'goal_resource_links', ['goal_id'], unique=False)
    op.create_index(op.f('ix_goal_resource_links_resource_id'), 'goal_resource_links', ['resource_id'], unique=False)
    op.create_table('resource_license_decisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('license_id', sa.UUID(), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('actor_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['license_id'], ['resource_licenses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_resource_license_decisions_actor_professional_id'), 'resource_license_decisions', ['actor_professional_id'], unique=False)
    op.create_index(op.f('ix_resource_license_decisions_license_id'), 'resource_license_decisions', ['license_id'], unique=False)
    op.create_table('program_resource_links',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['program_id'], ['intervention_programs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('program_id', 'resource_id', name='uq_program_resource_links_program_resource')
    )
    op.create_index(op.f('ix_program_resource_links_created_by_professional_id'), 'program_resource_links', ['created_by_professional_id'], unique=False)
    op.create_index(op.f('ix_program_resource_links_program_id'), 'program_resource_links', ['program_id'], unique=False)
    op.create_index(op.f('ix_program_resource_links_resource_id'), 'program_resource_links', ['resource_id'], unique=False)
    op.add_column('resources', sa.Column('publication_status', sa.String(length=16), server_default='draft', nullable=False))
    op.add_column('resources', sa.Column('content_sha256', sa.String(length=64), nullable=True))
    op.add_column('resources', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('resources', 'archived_at')
    op.drop_column('resources', 'content_sha256')
    op.drop_column('resources', 'publication_status')
    op.drop_index(op.f('ix_program_resource_links_resource_id'), table_name='program_resource_links')
    op.drop_index(op.f('ix_program_resource_links_program_id'), table_name='program_resource_links')
    op.drop_index(op.f('ix_program_resource_links_created_by_professional_id'), table_name='program_resource_links')
    op.drop_table('program_resource_links')
    op.drop_index(op.f('ix_resource_license_decisions_license_id'), table_name='resource_license_decisions')
    op.drop_index(op.f('ix_resource_license_decisions_actor_professional_id'), table_name='resource_license_decisions')
    op.drop_table('resource_license_decisions')
    op.drop_index(op.f('ix_goal_resource_links_resource_id'), table_name='goal_resource_links')
    op.drop_index(op.f('ix_goal_resource_links_goal_id'), table_name='goal_resource_links')
    op.drop_index(op.f('ix_goal_resource_links_created_by_professional_id'), table_name='goal_resource_links')
    op.drop_table('goal_resource_links')
    op.drop_index(op.f('ix_resource_licenses_resource_id'), table_name='resource_licenses')
    op.drop_index(op.f('ix_resource_licenses_declared_by_professional_id'), table_name='resource_licenses')
    op.drop_table('resource_licenses')
    op.drop_index(op.f('ix_resource_domain_links_resource_id'), table_name='resource_domain_links')
    op.drop_index(op.f('ix_resource_domain_links_domain_key'), table_name='resource_domain_links')
    op.drop_index(op.f('ix_resource_domain_links_created_by_professional_id'), table_name='resource_domain_links')
    op.drop_table('resource_domain_links')
    op.drop_index(op.f('ix_storage_cleanup_tasks_storage_key'), table_name='storage_cleanup_tasks')
    op.drop_index('ix_storage_cleanup_tasks_status_not_before', table_name='storage_cleanup_tasks')
    op.drop_index(op.f('ix_storage_cleanup_tasks_status'), table_name='storage_cleanup_tasks')
    op.drop_index(op.f('ix_storage_cleanup_tasks_created_by_professional_id'), table_name='storage_cleanup_tasks')
    op.drop_table('storage_cleanup_tasks')
