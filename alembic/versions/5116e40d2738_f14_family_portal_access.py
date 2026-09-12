"""f14_family_portal_access

Revision ID: 5116e40d2738
Revises: 0630221e18ba
Create Date: 2026-09-11 19:53:20.742675

Nota de revisão (curadoria obrigatória — plano §6.3): o autogenerate também
capturou o drift pré-existente do banco de dev (affiliates, app_notifications,
churn de FK em appointments, índices de battery_*, constraints de
financial_profiles/google_calendar_connections/notification_settings) — removido
desta revisão de propósito; segue como dívida para um ciclo de limpeza próprio.
Esta migration contém apenas a onda F14/M1: a coluna de época
professionals.family_portal_access_version (único preenchimento legado: 0 =
"nenhuma invalidação anterior", NÃO autoriza acesso) e as quatro tabelas do
portal da família (portals/recipients/events/grants) com constraints e índices
— incluindo o índice parcial único "um grant vivo por destinatário" para
PostgreSQL e SQLite. Nenhum consentimento/grant é fabricado.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5116e40d2738'
down_revision: Union[str, Sequence[str], None] = '0630221e18ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('family_portals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('owner_professional_id', sa.UUID(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('access_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('access_version >= 0', name='ck_family_portals_access_version'),
    sa.CheckConstraint('version >= 1', name='ck_family_portals_version'),
    sa.ForeignKeyConstraint(['owner_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('patient_id')
    )
    op.create_index(op.f('ix_family_portals_owner_professional_id'), 'family_portals', ['owner_professional_id'], unique=False)
    op.create_table('family_portal_recipients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('portal_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('appointments_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('authorization_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('authorization_version >= 0', name='ck_family_portal_recipient_authorization_version'),
    sa.CheckConstraint('caregiver_id IS NOT NULL OR active = false', name='ck_family_portal_recipient_link'),
    sa.CheckConstraint('version >= 1', name='ck_family_portal_recipient_version'),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['portal_id'], ['family_portals.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('portal_id', 'caregiver_id', name='uq_family_portal_recipient')
    )
    op.create_index(op.f('ix_family_portal_recipients_portal_id'), 'family_portal_recipients', ['portal_id'], unique=False)
    op.create_table('family_portal_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('portal_id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=True),
    sa.Column('actor_professional_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('authorization_version', sa.Integer(), nullable=True),
    sa.Column('grant_id', sa.UUID(), nullable=True),
    sa.Column('item_id', sa.UUID(), nullable=True),
    sa.Column('payload', sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('authorization_version IS NULL OR authorization_version >= 1', name='ck_family_portal_event_authorization_version'),
    sa.ForeignKeyConstraint(['actor_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['portal_id'], ['family_portals.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['family_portal_recipients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipient_id', 'authorization_version', name='uq_family_portal_event_authorization')
    )
    op.create_index('ix_family_portal_events_portal_timeline', 'family_portal_events', ['portal_id', 'occurred_at', 'id'], unique=False)
    op.create_table('family_portal_grants',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=False),
    sa.Column('authorization_event_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('owner_access_version', sa.Integer(), nullable=False),
    sa.Column('portal_access_version', sa.Integer(), nullable=False),
    sa.Column('created_by_professional_id', sa.UUID(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by_professional_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('expires_at > created_at', name='ck_family_portal_grants_expiry'),
    sa.CheckConstraint('owner_access_version >= 0', name='ck_family_portal_grants_owner_access_version'),
    sa.CheckConstraint('portal_access_version >= 0', name='ck_family_portal_grants_portal_access_version'),
    sa.ForeignKeyConstraint(['authorization_event_id'], ['family_portal_events.id'], ),
    sa.ForeignKeyConstraint(['created_by_professional_id'], ['professionals.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['family_portal_recipients.id'], ),
    sa.ForeignKeyConstraint(['revoked_by_professional_id'], ['professionals.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_family_portal_grants_recipient_created', 'family_portal_grants', ['recipient_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_family_portal_grants_token_hash'), 'family_portal_grants', ['token_hash'], unique=True)
    op.create_index('uq_family_portal_grants_active', 'family_portal_grants', ['recipient_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'), sqlite_where=sa.text('revoked_at IS NULL'))
    op.add_column('professionals', sa.Column('family_portal_access_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('professionals', 'family_portal_access_version')
    op.drop_index('uq_family_portal_grants_active', table_name='family_portal_grants', postgresql_where=sa.text('revoked_at IS NULL'), sqlite_where=sa.text('revoked_at IS NULL'))
    op.drop_index(op.f('ix_family_portal_grants_token_hash'), table_name='family_portal_grants')
    op.drop_index('ix_family_portal_grants_recipient_created', table_name='family_portal_grants')
    op.drop_table('family_portal_grants')
    op.drop_index('ix_family_portal_events_portal_timeline', table_name='family_portal_events')
    op.drop_table('family_portal_events')
    op.drop_index(op.f('ix_family_portal_recipients_portal_id'), table_name='family_portal_recipients')
    op.drop_table('family_portal_recipients')
    op.drop_index(op.f('ix_family_portals_owner_professional_id'), table_name='family_portals')
    op.drop_table('family_portals')
