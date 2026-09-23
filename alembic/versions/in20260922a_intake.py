"""C1 — digital pre-attendance requests and private files."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "in20260922a"
down_revision: str | Sequence[str] | None = "cr20260922a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("caregiver_id", sa.UUID(), nullable=True),
        sa.Column("caregiver_name_snapshot", sa.String(length=255), nullable=False, server_default="Responsável"),
        sa.Column("owner_professional_id", sa.UUID(), nullable=False),
        sa.Column("form_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("responses", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_professional_id", sa.UUID(), nullable=True),
        sa.Column("submitted_command_key", sa.String(length=128), nullable=True),
        sa.Column("submitted_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("review_command_key", sa.String(length=128), nullable=True),
        sa.Column("review_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("anamnese_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("selected_fields", sa.JSON(), nullable=False),
        sa.Column("selected_file_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_intake_requests_version"),
        sa.CheckConstraint("status IN ('draft','submitted','reviewed','cancelled')", name="ck_intake_requests_status"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["caregiver_id"], ["caregivers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_professional_id"], ["professionals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_intake_requests_patient_id", "intake_requests", ["patient_id"])
    op.create_index("ix_intake_requests_caregiver_id", "intake_requests", ["caregiver_id"])
    op.create_index("ix_intake_requests_owner_professional_id", "intake_requests", ["owner_professional_id"])
    op.create_index("uq_intake_requests_open_caregiver", "intake_requests", ["patient_id", "caregiver_id"], unique=True, postgresql_where=sa.text("status = 'draft'"), sqlite_where=sa.text("status = 'draft'"))

    op.create_table(
        "intake_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intake_request_id", sa.UUID(), nullable=False),
        sa.Column("caregiver_id", sa.UUID(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorization", sa.JSON(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_professional_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("expires_at > created_at", name="ck_intake_grants_expiry"),
        sa.ForeignKeyConstraint(["intake_request_id"], ["intake_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["caregiver_id"], ["caregivers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_professional_id"], ["professionals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_intake_grants_intake_request_id", "intake_grants", ["intake_request_id"])
    op.create_index("ix_intake_grants_token_hash", "intake_grants", ["token_hash"])
    op.create_index("uq_intake_grants_active_request", "intake_grants", ["intake_request_id"], unique=True, postgresql_where=sa.text("revoked_at IS NULL"), sqlite_where=sa.text("revoked_at IS NULL"))

    op.create_table(
        "intake_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intake_request_id", sa.UUID(), nullable=False),
        sa.Column("caregiver_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("incorporated_attachment_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 5242880", name="ck_intake_files_size"),
        sa.CheckConstraint("content_type IN ('application/pdf','image/jpeg','image/png')", name="ck_intake_files_type"),
        sa.ForeignKeyConstraint(["intake_request_id"], ["intake_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["caregiver_id"], ["caregivers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incorporated_attachment_id"], ["attachments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_intake_files_intake_request_id", "intake_files", ["intake_request_id"])

    op.create_table(
        "intake_audit_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("intake_request_id", sa.UUID(), nullable=False),
        sa.Column("patient_id", sa.UUID(), nullable=False),
        sa.Column("actor_professional_id", sa.UUID(), nullable=True),
        sa.Column("actor_caregiver_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["intake_request_id"], ["intake_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["actor_caregiver_id"], ["caregivers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_intake_audit_events_patient_id", "intake_audit_events", ["patient_id"])
    op.create_index("ix_intake_audit_events_request_time", "intake_audit_events", ["intake_request_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_intake_audit_events_request_time", table_name="intake_audit_events")
    op.drop_index("ix_intake_audit_events_patient_id", table_name="intake_audit_events")
    op.drop_table("intake_audit_events")
    op.drop_index("ix_intake_files_intake_request_id", table_name="intake_files")
    op.drop_table("intake_files")
    op.drop_index("uq_intake_grants_active_request", table_name="intake_grants")
    op.drop_index("ix_intake_grants_token_hash", table_name="intake_grants")
    op.drop_index("ix_intake_grants_intake_request_id", table_name="intake_grants")
    op.drop_table("intake_grants")
    op.drop_index("uq_intake_requests_open_caregiver", table_name="intake_requests")
    op.drop_index("ix_intake_requests_owner_professional_id", table_name="intake_requests")
    op.drop_index("ix_intake_requests_caregiver_id", table_name="intake_requests")
    op.drop_index("ix_intake_requests_patient_id", table_name="intake_requests")
    op.drop_table("intake_requests")
