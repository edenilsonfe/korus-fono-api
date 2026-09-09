"""patient care team

Revision ID: c4d5e6f7a8b9
Revises: 3f782f034deb
Create Date: 2026-09-09 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "3f782f034deb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "patient_sharing_consent_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("caregiver_id", sa.UUID(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("recorded_by_professional_id", sa.UUID(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('granted', 'withdrawn')",
            name="ck_patient_sharing_consent_decision",
        ),
        sa.ForeignKeyConstraint(["caregiver_id"], ["caregivers.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["recorded_by_professional_id"], ["professionals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_patient_sharing_consent_events_patient_id",
        "patient_sharing_consent_events",
        ["patient_id"],
    )

    op.create_table(
        "patient_care_team_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invited_by_professional_id", sa.UUID(), nullable=False),
        sa.Column("consent_event_id", sa.UUID(), nullable=False),
        sa.Column("invite_token_hash", sa.String(length=64), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_professional_id", sa.UUID(), nullable=True),
        sa.Column("revocation_reason", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint(
            "role IN ('supervisor', 'practitioner')",
            name="ck_patient_care_team_role",
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'declined', 'revoked', 'expired')",
            name="ck_patient_care_team_status",
        ),
        sa.ForeignKeyConstraint(
            ["consent_event_id"], ["patient_sharing_consent_events.id"]
        ),
        sa.ForeignKeyConstraint(["invited_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["revoked_by_professional_id"], ["professionals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patient_id", "professional_id", name="uq_patient_care_team_member"
        ),
    )
    op.create_index(
        "ix_patient_care_team_members_invite_token_hash",
        "patient_care_team_members",
        ["invite_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_patient_care_team_members_patient_id",
        "patient_care_team_members",
        ["patient_id"],
    )
    op.create_index(
        "ix_patient_care_team_members_professional_id",
        "patient_care_team_members",
        ["professional_id"],
    )
    op.create_index(
        "ix_patient_care_team_members_status",
        "patient_care_team_members",
        ["status"],
    )

    op.create_table(
        "patient_access_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("actor_professional_id", sa.UUID(), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_patient_access_events_action", "patient_access_events", ["action"]
    )
    op.create_index(
        "ix_patient_access_events_actor_professional_id",
        "patient_access_events",
        ["actor_professional_id"],
    )
    op.create_index(
        "ix_patient_access_events_occurred_at",
        "patient_access_events",
        ["occurred_at"],
    )
    op.create_index(
        "ix_patient_access_events_patient_id",
        "patient_access_events",
        ["patient_id"],
    )

    op.execute(
        sa.text(
            """
            INSERT INTO feature_flags
                (key, description, enabled_global, audience, created_at, updated_at)
            VALUES
                ('multidisciplinary_aba', 'Equipe multiprofissional e programas ABA', false, NULL, now(), now())
            ON CONFLICT (key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_flags WHERE key = 'multidisciplinary_aba'"))
    op.drop_index(
        "ix_patient_access_events_patient_id", table_name="patient_access_events"
    )
    op.drop_index(
        "ix_patient_access_events_occurred_at", table_name="patient_access_events"
    )
    op.drop_index(
        "ix_patient_access_events_actor_professional_id",
        table_name="patient_access_events",
    )
    op.drop_index("ix_patient_access_events_action", table_name="patient_access_events")
    op.drop_table("patient_access_events")
    op.drop_index(
        "ix_patient_care_team_members_status", table_name="patient_care_team_members"
    )
    op.drop_index(
        "ix_patient_care_team_members_professional_id",
        table_name="patient_care_team_members",
    )
    op.drop_index(
        "ix_patient_care_team_members_patient_id",
        table_name="patient_care_team_members",
    )
    op.drop_index(
        "ix_patient_care_team_members_invite_token_hash",
        table_name="patient_care_team_members",
    )
    op.drop_table("patient_care_team_members")
    op.drop_index(
        "ix_patient_sharing_consent_events_patient_id",
        table_name="patient_sharing_consent_events",
    )
    op.drop_table("patient_sharing_consent_events")
