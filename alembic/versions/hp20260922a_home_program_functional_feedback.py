"""Entrega A: retorno funcional opcional da família no programa de casa."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "hp20260922a"
down_revision: Union[str, Sequence[str], None] = "7c96e4a2d1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "home_program_tasks",
        sa.Column("functional_question", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        "ck_home_program_task_functional_question_length",
        "home_program_tasks",
        "functional_question IS NULL OR length(functional_question) <= 500",
    )

    op.add_column(
        "home_program_check_ins",
        sa.Column("functional_question", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "home_program_check_ins",
        sa.Column("functional_observation", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "home_program_check_ins",
        sa.Column("observation_context", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_home_program_check_in_functional_observation",
        "home_program_check_ins",
        "functional_observation IS NULL OR functional_observation IN "
        "('independent', 'with_support', 'not_observed', 'no_opportunity')",
    )
    op.create_check_constraint(
        "ck_home_program_check_in_observation_context",
        "home_program_check_ins",
        "observation_context IS NULL OR observation_context IN "
        "('home', 'school', 'other')",
    )

    op.add_column(
        "home_program_check_in_revisions",
        sa.Column("functional_question", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "home_program_check_in_revisions",
        sa.Column("functional_observation", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "home_program_check_in_revisions",
        sa.Column("observation_context", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_home_program_check_in_revision_functional_observation",
        "home_program_check_in_revisions",
        "functional_observation IS NULL OR functional_observation IN "
        "('independent', 'with_support', 'not_observed', 'no_opportunity')",
    )
    op.create_check_constraint(
        "ck_home_program_check_in_revision_observation_context",
        "home_program_check_in_revisions",
        "observation_context IS NULL OR observation_context IN "
        "('home', 'school', 'other')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_home_program_check_in_revision_observation_context",
        "home_program_check_in_revisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_home_program_check_in_revision_functional_observation",
        "home_program_check_in_revisions",
        type_="check",
    )
    op.drop_column("home_program_check_in_revisions", "observation_context")
    op.drop_column("home_program_check_in_revisions", "functional_observation")
    op.drop_column("home_program_check_in_revisions", "functional_question")

    op.drop_constraint(
        "ck_home_program_check_in_observation_context",
        "home_program_check_ins",
        type_="check",
    )
    op.drop_constraint(
        "ck_home_program_check_in_functional_observation",
        "home_program_check_ins",
        type_="check",
    )
    op.drop_column("home_program_check_ins", "observation_context")
    op.drop_column("home_program_check_ins", "functional_observation")
    op.drop_column("home_program_check_ins", "functional_question")

    op.drop_constraint(
        "ck_home_program_task_functional_question_length",
        "home_program_tasks",
        type_="check",
    )
    op.drop_column("home_program_tasks", "functional_question")
