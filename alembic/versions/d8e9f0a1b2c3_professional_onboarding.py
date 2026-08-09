"""professional onboarding state and stable demo patients

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_dismissed_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_viewed_demo_patient_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "professionals",
        sa.Column("onboarding_viewed_demo_result_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE patients SET is_demo = true "
            "WHERE name = 'Paciente demonstração'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE professionals SET onboarding_started_at = created_at "
            "WHERE onboarding_started_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE professionals AS p SET onboarding_completed_at = first_patient.created_at "
            "FROM ("
            "  SELECT professional_id, MIN(created_at) AS created_at "
            "  FROM patients WHERE is_demo = false GROUP BY professional_id"
            ") AS first_patient "
            "WHERE p.id = first_patient.professional_id "
            "AND p.onboarding_completed_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("professionals", "onboarding_viewed_demo_result_at")
    op.drop_column("professionals", "onboarding_viewed_demo_patient_at")
    op.drop_column("professionals", "onboarding_dismissed_until")
    op.drop_column("professionals", "onboarding_completed_at")
    op.drop_column("professionals", "onboarding_started_at")
    op.drop_column("professionals", "onboarding_version")
    op.drop_column("patients", "is_demo")
