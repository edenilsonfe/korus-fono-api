"""f6_patient_record_exports

Revision ID: 52d2079a38ae
Revises: 6bca409fe444
Create Date: 2026-09-11 06:39:02.542998

Nota de revisão (curadoria obrigatória — plano §6.3): mesmo drift pré-existente
do banco local (affiliates, app_notifications, appointments FK, battery_*,
financial_profiles, google_calendar_connections, notification_settings)
REMOVIDO desta revisão — pendente de ciclo próprio de reconciliação.

Contém apenas a tabela `patient_record_exports` (auditoria F6: requested
persistido antes da geração; generated/failed em transação independente) e
seus índices por patient_id / professional_id / requested_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '52d2079a38ae'
down_revision: Union[str, Sequence[str], None] = '6bca409fe444'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('patient_record_exports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('professional_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('format', sa.String(length=8), nullable=False),
    sa.Column('sections', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('from_date', sa.Date(), nullable=True),
    sa.Column('to_date', sa.Date(), nullable=True),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('record_counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('attachment_count', sa.Integer(), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['professional_id'], ['professionals.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_patient_record_exports_patient_id'), 'patient_record_exports', ['patient_id'], unique=False)
    op.create_index(op.f('ix_patient_record_exports_professional_id'), 'patient_record_exports', ['professional_id'], unique=False)
    op.create_index(op.f('ix_patient_record_exports_requested_at'), 'patient_record_exports', ['requested_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_patient_record_exports_requested_at'), table_name='patient_record_exports')
    op.drop_index(op.f('ix_patient_record_exports_professional_id'), table_name='patient_record_exports')
    op.drop_index(op.f('ix_patient_record_exports_patient_id'), table_name='patient_record_exports')
    op.drop_table('patient_record_exports')
