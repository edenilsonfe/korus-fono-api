"""internal clinic finance

Revision ID: f1e2d3c4b5a6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1e2d3c4b5a6"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("sessions", sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_sessions_appointment_id", "sessions", "appointments", ["appointment_id"], ["id"], ondelete="SET NULL"
    )
    op.create_unique_constraint("uq_sessions_appointment_id", "sessions", ["appointment_id"])

    op.create_table(
        "financial_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_type", sa.String(2), server_default="PF", nullable=False),
        sa.Column("legal_name", sa.String(180), server_default="", nullable=False),
        sa.Column("trade_name", sa.String(180), server_default="", nullable=False),
        sa.Column("document", sa.String(20), server_default="", nullable=False),
        sa.Column("council_registration", sa.String(80), server_default="", nullable=False),
        sa.Column("municipal_registration", sa.String(80), server_default="", nullable=False),
        sa.Column("address_line", sa.String(255), server_default="", nullable=False),
        sa.Column("city", sa.String(100), server_default="", nullable=False),
        sa.Column("state", sa.String(2), server_default="", nullable=False),
        sa.Column("postal_code", sa.String(12), server_default="", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("professional_id"),
    )
    op.create_index("ix_financial_profiles_professional_id", "financial_profiles", ["professional_id"])

    op.create_table(
        "financial_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("professional_id", "kind", "name", name="uq_fin_category_name"),
    )
    op.create_index("ix_financial_categories_professional_id", "financial_categories", ["professional_id"])
    op.create_index("ix_financial_categories_kind", "financial_categories", ["kind"])

    op.create_table(
        "financial_payment_methods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("professional_id", "name", name="uq_fin_payment_method_name"),
    )
    op.create_index("ix_financial_payment_methods_professional_id", "financial_payment_methods", ["professional_id"])

    op.create_table(
        "financial_services",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("duration", sa.Integer(), server_default="50", nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["financial_categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_financial_services_professional_id", "financial_services", ["professional_id"])

    op.create_table(
        "financial_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("sessions_count", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("validity_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["financial_services.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_financial_packages_professional_id", "financial_packages", ["professional_id"])

    op.create_table(
        "financial_receivables",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("patient_name_snapshot", sa.String(180), server_default="", nullable=False),
        sa.Column("payer_name", sa.String(180), nullable=False),
        sa.Column("payer_document", sa.String(20), server_default="", nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("competence_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="open", nullable=False),
        sa.Column("origin", sa.String(24), server_default="manual", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["category_id"], ["financial_categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("professional_id", "patient_id", "issue_date", "competence_date", "due_date", "status"):
        op.create_index(f"ix_financial_receivables_{column}", "financial_receivables", [column])

    op.create_table(
        "financial_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("patient_name_snapshot", sa.String(180), server_default="", nullable=False),
        sa.Column("payer_name", sa.String(180), nullable=False),
        sa.Column("payer_document", sa.String(20), server_default="", nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="confirmed", nullable=False),
        sa.Column("receipt_number", sa.String(40), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["method_id"], ["financial_payment_methods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_number"),
    )
    for column in ("professional_id", "patient_id", "payment_date", "status"):
        op.create_index(f"ix_financial_payments_{column}", "financial_payments", [column])

    op.create_table(
        "financial_receivable_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receivable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_type", sa.String(24), server_default="service", nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["receivable_id"], ["financial_receivables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["financial_services.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("appointment_id"),
    )
    op.create_index("ix_financial_receivable_items_receivable_id", "financial_receivable_items", ["receivable_id"])

    op.create_table(
        "financial_payment_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receivable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["payment_id"], ["financial_payments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["receivable_id"], ["financial_receivables.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_id", "receivable_id", name="uq_payment_receivable"),
    )
    op.create_index("ix_financial_payment_allocations_payment_id", "financial_payment_allocations", ["payment_id"])
    op.create_index("ix_financial_payment_allocations_receivable_id", "financial_payment_allocations", ["receivable_id"])

    op.create_table(
        "financial_payables",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("supplier_name", sa.String(180), server_default="", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("competence_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="open", nullable=False),
        sa.Column("recurrence", sa.String(24), nullable=True),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["financial_categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("professional_id", "competence_date", "due_date", "status"):
        op.create_index(f"ix_financial_payables_{column}", "financial_payables", [column])

    op.create_table(
        "financial_payable_settlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="confirmed", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["payable_id"], ["financial_payables.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["method_id"], ["financial_payment_methods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("payable_id", "professional_id", "payment_date"):
        op.create_index(f"ix_financial_payable_settlements_{column}", "financial_payable_settlements", [column])

    op.create_table(
        "financial_patient_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("receivable_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("patient_name_snapshot", sa.String(180), nullable=False),
        sa.Column("package_name_snapshot", sa.String(120), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("sessions_included", sa.Integer(), nullable=False),
        sa.Column("sessions_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("agreed_price_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["package_id"], ["financial_packages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["receivable_id"], ["financial_receivables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("professional_id", "patient_id", "status"):
        op.create_index(f"ix_financial_patient_packages_{column}", "financial_patient_packages", [column])

    op.create_table(
        "financial_package_usages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("used_on", sa.Date(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["patient_package_id"], ["financial_patient_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("appointment_id"),
    )
    op.create_index("ix_financial_package_usages_patient_package_id", "financial_package_usages", ["patient_package_id"])

    op.create_table(
        "financial_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("professional_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_financial_audit_events_professional_id", "financial_audit_events", ["professional_id"])
    op.create_index("ix_financial_audit_events_entity_id", "financial_audit_events", ["entity_id"])


def downgrade() -> None:
    for table in (
        "financial_audit_events",
        "financial_package_usages",
        "financial_patient_packages",
        "financial_payable_settlements",
        "financial_payables",
        "financial_payment_allocations",
        "financial_receivable_items",
        "financial_payments",
        "financial_receivables",
        "financial_packages",
        "financial_services",
        "financial_payment_methods",
        "financial_categories",
        "financial_profiles",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_sessions_appointment_id", "sessions", type_="unique")
    op.drop_constraint("fk_sessions_appointment_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "appointment_id")
