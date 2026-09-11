"""f16_home_programs

Revision ID: 0630221e18ba
Revises: 73b5bac60d26
Create Date: 2026-09-11 15:28:43.344518

Nota de revisão (curadoria obrigatória — plano §6.3): o autogenerate também
capturou o drift pré-existente do banco de dev (affiliates, app_notifications,
churn de FK em appointments, índices de battery_*, constraints de
financial_profiles/google_calendar_connections/notification_settings) — removido
desta revisão de propósito; segue como dívida para um ciclo de limpeza próprio.
Esta migration contém apenas a onda F16: as tabelas home_programs,
home_program_grants, home_program_tasks, home_program_check_ins,
home_program_check_in_revisions, home_program_events, home_program_task_resources
e home_program_photos (com índices parciais "grant ativo" e "foto vigente").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0630221e18ba'
down_revision: Union[str, Sequence[str], None] = '73b5bac60d26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('home_programs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('starts_on', sa.Date(), nullable=False),
    sa.Column('ends_on', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name='ck_home_program_status'),
    sa.CheckConstraint('ends_on >= starts_on', name='ck_home_program_period'),
    sa.CheckConstraint('version >= 1', name='ck_home_program_version'),
    sa.ForeignKeyConstraint(['archived_by_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_home_programs_patient_id'), 'home_programs', ['patient_id'], unique=False)
    op.create_index(op.f('ix_home_programs_status'), 'home_programs', ['status'], unique=False)
    op.create_table('home_program_grants',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=True),
    sa.Column('caregiver_name_snapshot', sa.String(length=255), nullable=False),
    sa.Column('caregiver_relation_snapshot', sa.String(length=64), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=False),
    sa.Column('consent_event_id', sa.UUID(), nullable=True),
    sa.Column('family_authorization', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['consent_event_id'], ['patient_sharing_consent_events.id'], ),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['program_id'], ['home_programs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revoked_by_professional_id'], ['professionals.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_home_program_grants_program_id'), 'home_program_grants', ['program_id'], unique=False)
    op.create_index(op.f('ix_home_program_grants_token_hash'), 'home_program_grants', ['token_hash'], unique=True)
    op.create_index('uq_home_program_grants_active', 'home_program_grants', ['program_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'), sqlite_where=sa.text('revoked_at IS NULL'))
    op.create_table('home_program_tasks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('client_task_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('instructions', sa.Text(), nullable=False),
    sa.Column('due_on', sa.Date(), nullable=False),
    sa.Column('goal_id', sa.UUID(), nullable=True),
    sa.Column('intervention_program_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(goal_id IS NOT NULL) <> (intervention_program_id IS NOT NULL)', name='ck_home_program_task_target_xor'),
    sa.ForeignKeyConstraint(['goal_id'], ['goals.id'], ),
    sa.ForeignKeyConstraint(['intervention_program_id'], ['intervention_programs.id'], ),
    sa.ForeignKeyConstraint(['program_id'], ['home_programs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('program_id', 'client_task_id', name='uq_home_program_task_client_id'),
    sa.UniqueConstraint('program_id', 'position', name='uq_home_program_task_position')
    )
    op.create_index(op.f('ix_home_program_tasks_program_id'), 'home_program_tasks', ['program_id'], unique=False)
    op.create_table('home_program_check_ins',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('grant_id', sa.UUID(), nullable=True),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('responded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name='ck_home_program_check_in_version'),
    sa.ForeignKeyConstraint(['grant_id'], ['home_program_grants.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['program_id'], ['home_programs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['home_program_tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', name='uq_home_program_check_in_task')
    )
    op.create_index(op.f('ix_home_program_check_ins_program_id'), 'home_program_check_ins', ['program_id'], unique=False)
    op.create_index(op.f('ix_home_program_check_ins_task_id'), 'home_program_check_ins', ['task_id'], unique=False)
    op.create_table('home_program_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('grant_id', sa.UUID(), nullable=True),
    sa.Column('task_id', sa.UUID(), nullable=True),
    sa.Column('check_in_id', sa.UUID(), nullable=True),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=True),
    sa.Column('client_record_id', sa.UUID(), nullable=True),
    sa.Column('result_version', sa.Integer(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['grant_id'], ['home_program_grants.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['program_id'], ['home_programs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('grant_id', 'client_record_id', name='uq_home_program_event_client_record')
    )
    op.create_index(op.f('ix_home_program_events_event_type'), 'home_program_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_home_program_events_occurred_at'), 'home_program_events', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_home_program_events_program_id'), 'home_program_events', ['program_id'], unique=False)
    op.create_table('home_program_task_resources',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('resource_id', sa.UUID(), nullable=False),
    sa.Column('title_snapshot', sa.String(length=255), nullable=False),
    sa.Column('resource_sha256', sa.String(length=64), nullable=True),
    sa.Column('license_id', sa.UUID(), nullable=True),
    sa.Column('license_version', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['license_id'], ['resource_licenses.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['home_program_tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'resource_id', name='uq_home_program_task_resource')
    )
    op.create_index(op.f('ix_home_program_task_resources_resource_id'), 'home_program_task_resources', ['resource_id'], unique=False)
    op.create_index(op.f('ix_home_program_task_resources_task_id'), 'home_program_task_resources', ['task_id'], unique=False)
    op.create_table('home_program_check_in_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('check_in_id', sa.UUID(), nullable=False),
    sa.Column('grant_id', sa.UUID(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['check_in_id'], ['home_program_check_ins.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['grant_id'], ['home_program_grants.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_home_program_check_in_revisions_check_in_id'), 'home_program_check_in_revisions', ['check_in_id'], unique=False)
    op.create_table('home_program_photos',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('check_in_id', sa.UUID(), nullable=False),
    sa.Column('program_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=True),
    sa.Column('content_type', sa.String(length=64), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'ready', 'deleted')", name='ck_home_program_photo_status'),
    sa.ForeignKeyConstraint(['check_in_id'], ['home_program_check_ins.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['program_id'], ['home_programs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_home_program_photos_check_in_id'), 'home_program_photos', ['check_in_id'], unique=False)
    op.create_index(op.f('ix_home_program_photos_program_id'), 'home_program_photos', ['program_id'], unique=False)
    op.create_index(op.f('ix_home_program_photos_status'), 'home_program_photos', ['status'], unique=False)
    op.create_index('uq_home_program_photos_current', 'home_program_photos', ['check_in_id'], unique=True, postgresql_where=sa.text("status <> 'deleted'"), sqlite_where=sa.text("status <> 'deleted'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_home_program_photos_current', table_name='home_program_photos', postgresql_where=sa.text("status <> 'deleted'"), sqlite_where=sa.text("status <> 'deleted'"))
    op.drop_index(op.f('ix_home_program_photos_status'), table_name='home_program_photos')
    op.drop_index(op.f('ix_home_program_photos_program_id'), table_name='home_program_photos')
    op.drop_index(op.f('ix_home_program_photos_check_in_id'), table_name='home_program_photos')
    op.drop_table('home_program_photos')
    op.drop_index(op.f('ix_home_program_check_in_revisions_check_in_id'), table_name='home_program_check_in_revisions')
    op.drop_table('home_program_check_in_revisions')
    op.drop_index(op.f('ix_home_program_task_resources_task_id'), table_name='home_program_task_resources')
    op.drop_index(op.f('ix_home_program_task_resources_resource_id'), table_name='home_program_task_resources')
    op.drop_table('home_program_task_resources')
    op.drop_index(op.f('ix_home_program_events_program_id'), table_name='home_program_events')
    op.drop_index(op.f('ix_home_program_events_occurred_at'), table_name='home_program_events')
    op.drop_index(op.f('ix_home_program_events_event_type'), table_name='home_program_events')
    op.drop_table('home_program_events')
    op.drop_index(op.f('ix_home_program_check_ins_task_id'), table_name='home_program_check_ins')
    op.drop_index(op.f('ix_home_program_check_ins_program_id'), table_name='home_program_check_ins')
    op.drop_table('home_program_check_ins')
    op.drop_index(op.f('ix_home_program_tasks_program_id'), table_name='home_program_tasks')
    op.drop_table('home_program_tasks')
    op.drop_index('uq_home_program_grants_active', table_name='home_program_grants', postgresql_where=sa.text('revoked_at IS NULL'), sqlite_where=sa.text('revoked_at IS NULL'))
    op.drop_index(op.f('ix_home_program_grants_token_hash'), table_name='home_program_grants')
    op.drop_index(op.f('ix_home_program_grants_program_id'), table_name='home_program_grants')
    op.drop_table('home_program_grants')
    op.drop_index(op.f('ix_home_programs_status'), table_name='home_programs')
    op.drop_index(op.f('ix_home_programs_patient_id'), table_name='home_programs')
    op.drop_table('home_programs')
