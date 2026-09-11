"""f20_school_delivery_receipt

Revision ID: 6bca409fe444
Revises: 4147477648e9
Create Date: 2026-09-11 06:00:34.153398

Nota de revisão (curadoria obrigatória — plano §6.3): mesmo drift pré-existente
do banco local (affiliates, app_notifications, appointments FK, battery_*,
financial_profiles, google_calendar_connections, notification_settings)
REMOVIDO desta revisão — pendente de ciclo próprio de reconciliação.

Contém apenas as colunas escolares/recibo em `report_deliveries`:
recipient_kind (server_default 'standard' — entregas legadas), school_*,
school_authorization (JSONB restrito), authorization_recorded_at e
received_at/received_by_name/received_by_role (nuláveis; escritos só na
entrega escolar com confirmação de recebimento).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6bca409fe444'
down_revision: Union[str, Sequence[str], None] = '4147477648e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('report_deliveries', sa.Column('recipient_kind', sa.String(length=16), server_default='standard', nullable=False))
    op.add_column('report_deliveries', sa.Column('school_name', sa.String(length=160), nullable=True))
    op.add_column('report_deliveries', sa.Column('school_recipient_name', sa.String(length=160), nullable=True))
    op.add_column('report_deliveries', sa.Column('school_authorization', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('report_deliveries', sa.Column('authorization_recorded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('report_deliveries', sa.Column('received_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('report_deliveries', sa.Column('received_by_name', sa.String(length=160), nullable=True))
    op.add_column('report_deliveries', sa.Column('received_by_role', sa.String(length=120), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('report_deliveries', 'received_by_role')
    op.drop_column('report_deliveries', 'received_by_name')
    op.drop_column('report_deliveries', 'received_at')
    op.drop_column('report_deliveries', 'authorization_recorded_at')
    op.drop_column('report_deliveries', 'school_authorization')
    op.drop_column('report_deliveries', 'school_recipient_name')
    op.drop_column('report_deliveries', 'school_name')
    op.drop_column('report_deliveries', 'recipient_kind')
