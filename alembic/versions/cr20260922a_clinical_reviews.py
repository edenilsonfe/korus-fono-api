"""B1/D1 clinical review and structured discharge documents."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "cr20260922a"
down_revision = "hp20260922a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clinical_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("goal_decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("next_review_on", sa.Date(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_professional_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_review_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completion_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("completion_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("discharge_on", sa.Date(), nullable=True),
        sa.Column("discharge_reason", sa.String(length=2000), nullable=True),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("family_guidance", sa.Text(), nullable=True),
        sa.Column("return_recommended", sa.Boolean(), nullable=True),
        sa.Column("return_on", sa.Date(), nullable=True),
        sa.Column("home_program_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("agenda_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("appointment_decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("family_report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('periodic', 'discharge')", name="ck_clinical_review_kind"),
        sa.CheckConstraint("status IN ('draft', 'completed', 'cancelled')", name="ck_clinical_review_status"),
        sa.CheckConstraint("version >= 1", name="ck_clinical_review_version"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["completed_by_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["supersedes_review_id"], ["clinical_reviews.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["family_report_id"], ["ai_reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "completion_idempotency_key", name="uq_clinical_review_completion_key"),
    )
    op.create_index("ix_clinical_reviews_patient_id", "clinical_reviews", ["patient_id"])
    op.create_index("ix_clinical_reviews_author_professional_id", "clinical_reviews", ["author_professional_id"])
    op.create_index("ix_clinical_reviews_status", "clinical_reviews", ["status"])
    op.create_index("ix_clinical_reviews_patient_status", "clinical_reviews", ["patient_id", "status"])
    op.create_index("ix_clinical_reviews_due", "clinical_reviews", ["patient_id", "next_review_on"])
    op.create_index("ix_clinical_reviews_supersedes_review_id", "clinical_reviews", ["supersedes_review_id"])


def downgrade() -> None:
    op.drop_index("ix_clinical_reviews_supersedes_review_id", table_name="clinical_reviews")
    op.drop_index("ix_clinical_reviews_due", table_name="clinical_reviews")
    op.drop_index("ix_clinical_reviews_patient_status", table_name="clinical_reviews")
    op.drop_index("ix_clinical_reviews_status", table_name="clinical_reviews")
    op.drop_index("ix_clinical_reviews_author_professional_id", table_name="clinical_reviews")
    op.drop_index("ix_clinical_reviews_patient_id", table_name="clinical_reviews")
    op.drop_table("clinical_reviews")
