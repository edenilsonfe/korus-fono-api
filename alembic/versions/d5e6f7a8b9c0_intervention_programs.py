"""intervention programs and measurements

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intervention_programs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("created_by_professional_id", sa.UUID(), nullable=False),
        sa.Column("goal_id", sa.UUID(), nullable=True),
        sa.Column("approach", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("operational_definition", sa.Text(), nullable=False),
        sa.Column("teaching_strategy", sa.Text(), nullable=False),
        sa.Column("mastery_percent", sa.Integer(), nullable=False),
        sa.Column("mastery_consecutive_sessions", sa.Integer(), nullable=False),
        sa.Column("generalization_criterion", sa.Text(), nullable=True),
        sa.Column("maintenance_criterion", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_professional_id", sa.UUID(), nullable=True),
        sa.Column("replaces_program_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("approach = 'aba'", name="ck_intervention_program_approach"),
        sa.CheckConstraint(
            "mastery_consecutive_sessions BETWEEN 1 AND 20",
            name="ck_intervention_program_mastery_sessions",
        ),
        sa.CheckConstraint(
            "mastery_percent BETWEEN 1 AND 100",
            name="ck_intervention_program_mastery_percent",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'mastered', 'archived')",
            name="ck_intervention_program_status",
        ),
        sa.ForeignKeyConstraint(["closed_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["created_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["replaces_program_id"], ["intervention_programs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_intervention_programs_patient_id",
        "intervention_programs",
        ["patient_id"],
    )
    op.create_index(
        "ix_intervention_programs_status",
        "intervention_programs",
        ["status"],
    )

    op.create_table(
        "program_measurements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_record_id", sa.UUID(), nullable=False),
        sa.Column("program_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("recorded_by_professional_id", sa.UUID(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("participation_status", sa.String(length=24), nullable=False),
        sa.Column("opportunities", sa.Integer(), nullable=False),
        sa.Column("independent", sa.Integer(), nullable=False),
        sa.Column("prompted", sa.Integer(), nullable=False),
        sa.Column("incorrect", sa.Integer(), nullable=False),
        sa.Column("no_response", sa.Integer(), nullable=False),
        sa.Column("prompt_counts", postgresql.JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by_professional_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=1000), nullable=True),
        sa.Column("replaces_measurement_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "(participation_status = 'participated' AND opportunities > 0 "
            "AND independent + prompted + incorrect + no_response = opportunities) "
            "OR (participation_status <> 'participated' AND opportunities = 0 "
            "AND independent = 0 AND prompted = 0 AND incorrect = 0 "
            "AND no_response = 0)",
            name="ck_program_measurement_consistent_counts",
        ),
        sa.CheckConstraint(
            "opportunities >= 0 AND independent >= 0 AND prompted >= 0 "
            "AND incorrect >= 0 AND no_response >= 0",
            name="ck_program_measurement_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "participation_status IN "
            "('participated', 'declined', 'withdrew', 'not_observed')",
            name="ck_program_measurement_participation",
        ),
        sa.CheckConstraint(
            "review_status IN ('submitted', 'approved', 'voided')",
            name="ck_program_measurement_review_status",
        ),
        sa.ForeignKeyConstraint(["program_id"], ["intervention_programs.id"]),
        sa.ForeignKeyConstraint(["recorded_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(
            ["replaces_measurement_id"], ["program_measurements.id"]
        ),
        sa.ForeignKeyConstraint(["reviewed_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recorded_by_professional_id",
            "client_record_id",
            name="uq_program_measurement_client_record",
        ),
        sa.UniqueConstraint(
            "replaces_measurement_id", name="uq_program_measurement_replacement"
        ),
    )
    op.create_index(
        "ix_program_measurements_program_id",
        "program_measurements",
        ["program_id"],
    )
    op.create_index(
        "ix_program_measurements_recorded_at",
        "program_measurements",
        ["recorded_at"],
    )
    op.create_index(
        "ix_program_measurements_recorded_by_professional_id",
        "program_measurements",
        ["recorded_by_professional_id"],
    )
    op.create_index(
        "ix_program_measurements_review_status",
        "program_measurements",
        ["review_status"],
    )
    op.create_index(
        "ix_program_measurements_session_id",
        "program_measurements",
        ["session_id"],
    )
    op.create_index(
        "uq_program_measurement_current_session",
        "program_measurements",
        ["program_id", "session_id"],
        unique=True,
        postgresql_where=sa.text("review_status <> 'voided'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_program_measurement_current_session", table_name="program_measurements"
    )
    op.drop_index(
        "ix_program_measurements_session_id", table_name="program_measurements"
    )
    op.drop_index(
        "ix_program_measurements_review_status", table_name="program_measurements"
    )
    op.drop_index(
        "ix_program_measurements_recorded_by_professional_id",
        table_name="program_measurements",
    )
    op.drop_index(
        "ix_program_measurements_recorded_at", table_name="program_measurements"
    )
    op.drop_index(
        "ix_program_measurements_program_id", table_name="program_measurements"
    )
    op.drop_table("program_measurements")
    op.drop_index("ix_intervention_programs_status", table_name="intervention_programs")
    op.drop_index(
        "ix_intervention_programs_patient_id", table_name="intervention_programs"
    )
    op.drop_table("intervention_programs")
