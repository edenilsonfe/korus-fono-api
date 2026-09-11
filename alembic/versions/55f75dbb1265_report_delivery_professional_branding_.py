"""report delivery, professional branding and reassessment settings

Revision ID: 55f75dbb1265
Revises: 876ae1016496
Create Date: 2026-09-11 02:37:55.265783

Nota de revisão: o autogenerate também detectou divergências pré-existentes do
banco local (índices/constraints de affiliates, battery_*, financial_profiles,
google_calendar_connections, appointments e notification_settings) que NÃO
pertencem a esta entrega e foram removidas desta revisão para tratamento à parte.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55f75dbb1265'
down_revision: Union[str, Sequence[str], None] = '876ae1016496'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """F1 (entregas de relatório), F2 (identidade) e F13 (reavaliação/faltas)."""
    op.create_table('report_deliveries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('professional_id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('recipient_label', sa.String(length=255), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_status', sa.String(length=16), nullable=False),
    sa.Column('last_error', sa.String(length=255), nullable=True),
    sa.Column('provider_message_id', sa.String(length=128), nullable=True),
    sa.Column('view_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('download_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('first_viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['professional_id'], ['professionals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_id'], ['ai_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_report_deliveries_patient_id'), 'report_deliveries', ['patient_id'], unique=False)
    op.create_index(op.f('ix_report_deliveries_professional_id'), 'report_deliveries', ['professional_id'], unique=False)
    op.create_index(op.f('ix_report_deliveries_report_id'), 'report_deliveries', ['report_id'], unique=False)
    op.create_index(op.f('ix_report_deliveries_token_hash'), 'report_deliveries', ['token_hash'], unique=True)
    op.add_column('notification_settings', sa.Column('reassessment_reminder_months', sa.Integer(), nullable=True))
    op.add_column('notification_settings', sa.Column('no_show_policy', sa.String(length=400), nullable=True))
    op.add_column('professionals', sa.Column('branding_logo_key', sa.String(length=512), nullable=True))
    op.add_column('professionals', sa.Column('branding_signature_key', sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('professionals', 'branding_signature_key')
    op.drop_column('professionals', 'branding_logo_key')
    op.drop_column('notification_settings', 'no_show_policy')
    op.drop_column('notification_settings', 'reassessment_reminder_months')
    op.drop_index(op.f('ix_report_deliveries_token_hash'), table_name='report_deliveries')
    op.drop_index(op.f('ix_report_deliveries_report_id'), table_name='report_deliveries')
    op.drop_index(op.f('ix_report_deliveries_professional_id'), table_name='report_deliveries')
    op.drop_index(op.f('ix_report_deliveries_patient_id'), table_name='report_deliveries')
    op.drop_table('report_deliveries')
