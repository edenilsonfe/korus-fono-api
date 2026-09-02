"""Add affiliate program, immutable attribution and append-only ledger.

Revision ID: t1u2v3w4x5y6
Revises: s0t1u2v3w4x5
"""

import uuid
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "t1u2v3w4x5y6"
down_revision = "s0t1u2v3w4x5"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "affiliate_policies",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("terms_version", sa.String(64), nullable=False),
        sa.Column("referral_discount_bps", sa.Integer(), nullable=False),
        sa.Column("commission_bps", sa.Integer(), nullable=False),
        sa.Column("customer_reward_monthly_cents", sa.Integer(), nullable=False),
        sa.Column("customer_reward_quarterly_cents", sa.Integer(), nullable=False),
        sa.Column("customer_reward_yearly_cents", sa.Integer(), nullable=False),
        sa.Column("attribution_window_days", sa.Integer(), nullable=False),
        sa.Column("cooling_off_days", sa.Integer(), nullable=False),
        sa.Column("payout_minimum_cents", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('customer', 'partner')", name="ck_affiliate_policy_mode"),
        sa.CheckConstraint(
            "status IN ('draft', 'scheduled', 'active', 'retired')",
            name="ck_affiliate_policy_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["professionals.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("mode", "version", name="uq_affiliate_policy_mode_version"),
    )
    op.create_index("ix_affiliate_policies_mode", "affiliate_policies", ["mode"])
    op.create_index("ix_affiliate_policies_status", "affiliate_policies", ["status"])
    op.create_index(
        "uq_affiliate_policy_active_mode",
        "affiliate_policies",
        ["mode"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "affiliate_participants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("professional_id", UUID, nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("public_name", sa.String(120), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("customer_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("partner_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("customer_terms_version", sa.String(64), nullable=True),
        sa.Column("customer_terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("partner_terms_version", sa.String(64), nullable=True),
        sa.Column("partner_terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commission_override_bps", sa.Integer(), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'deactivated', 'closed')",
            name="ck_affiliate_participant_status",
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("professional_id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_affiliate_participants_email", "affiliate_participants", ["email"])
    op.create_index(
        "ix_affiliate_participants_professional_id",
        "affiliate_participants",
        ["professional_id"],
    )
    op.create_index("ix_affiliate_participants_status", "affiliate_participants", ["status"])

    op.create_table(
        "affiliate_codes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("code", sa.String(48), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("terms_version", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('customer', 'partner')", name="ck_affiliate_code_mode"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_affiliate_code_status"),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_affiliate_codes_code", "affiliate_codes", ["code"])
    op.create_index("ix_affiliate_codes_mode", "affiliate_codes", ["mode"])
    op.create_index("ix_affiliate_codes_participant_id", "affiliate_codes", ["participant_id"])

    op.create_table(
        "affiliate_referrals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("code_id", UUID, nullable=False),
        sa.Column("referred_professional_id", UUID, nullable=False),
        sa.Column("policy_id", UUID, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("review_state", sa.String(20), nullable=False),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column("benefit_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('customer', 'partner')", name="ck_affiliate_referral_mode"),
        sa.CheckConstraint(
            "status IN ('registered', 'converted', 'rejected')",
            name="ck_affiliate_referral_status",
        ),
        sa.CheckConstraint(
            "review_state IN ('clear', 'manual_review', 'approved', 'rejected')",
            name="ck_affiliate_referral_review_state",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["code_id"], ["affiliate_codes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["policy_id"], ["affiliate_policies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["referred_professional_id"], ["professionals.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("referred_professional_id"),
    )
    op.create_index("ix_affiliate_referrals_mode", "affiliate_referrals", ["mode"])
    op.create_index("ix_affiliate_referrals_participant_id", "affiliate_referrals", ["participant_id"])
    op.create_index("ix_affiliate_referrals_review_state", "affiliate_referrals", ["review_state"])
    op.create_index("ix_affiliate_referrals_source_fingerprint", "affiliate_referrals", ["source_fingerprint"])
    op.create_index("ix_affiliate_referrals_status", "affiliate_referrals", ["status"])

    op.create_table(
        "affiliate_rewards",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("referral_id", UUID, nullable=False),
        sa.Column("source_payment_id", sa.String(255), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("gross_cents", sa.Integer(), nullable=False),
        sa.Column("external_revenue_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="BRL"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'coolingOff', 'available', 'reserved', 'credited', "
            "'paid', 'reversed', 'voided')",
            name="ck_affiliate_reward_state",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["referral_id"], ["affiliate_referrals.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("referral_id", "source_payment_id", "kind", name="uq_affiliate_reward_source"),
    )
    op.create_index("ix_affiliate_rewards_participant_id", "affiliate_rewards", ["participant_id"])
    op.create_index("ix_affiliate_rewards_source_payment_id", "affiliate_rewards", ["source_payment_id"])
    op.create_index("ix_affiliate_rewards_state", "affiliate_rewards", ["state"])

    op.create_table(
        "affiliate_fiscal_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("person_type", sa.String(2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("legal_name", sa.String(255), nullable=False),
        sa.Column("document_fingerprint", sa.String(64), nullable=False),
        sa.Column("document_masked", sa.String(24), nullable=False),
        sa.Column("encrypted_document", sa.Text(), nullable=False),
        sa.Column("pix_key_type", sa.String(16), nullable=False),
        sa.Column("pix_key_masked", sa.String(255), nullable=False),
        sa.Column("pix_key_fingerprint", sa.String(64), nullable=False),
        sa.Column("encrypted_pix_key", sa.Text(), nullable=False),
        sa.Column("pix_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("person_type IN ('pf', 'pj')", name="ck_affiliate_fiscal_person_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')",
            name="ck_affiliate_fiscal_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approved_by_id"], ["professionals.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("participant_id", "version", name="uq_affiliate_fiscal_version"),
    )
    op.create_index("ix_affiliate_fiscal_profiles_participant_id", "affiliate_fiscal_profiles", ["participant_id"])
    op.create_index("ix_affiliate_fiscal_profiles_status", "affiliate_fiscal_profiles", ["status"])

    op.create_table(
        "affiliate_payout_batches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("competence", sa.String(7), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_by_id", UUID, nullable=False),
        sa.Column("approved_by_id", UUID, nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'processing', 'paid', 'failed')",
            name="ck_affiliate_payout_batch_status",
        ),
        sa.ForeignKeyConstraint(["prepared_by_id"], ["professionals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by_id"], ["professionals.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_affiliate_payout_batches_competence", "affiliate_payout_batches", ["competence"])
    op.create_index("ix_affiliate_payout_batches_status", "affiliate_payout_batches", ["status"])

    op.create_table(
        "affiliate_payout_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("fiscal_profile_id", UUID, nullable=False),
        sa.Column("batch_id", UUID, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("gross_cents", sa.Integer(), nullable=False),
        sa.Column("withholding_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fee_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("net_cents", sa.Integer(), nullable=False),
        sa.Column("provider_transfer_id", sa.String(255), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellable_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('requested', 'batched', 'approved', 'processing', 'paid', 'failed', 'canceled')",
            name="ck_affiliate_payout_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["fiscal_profile_id"], ["affiliate_fiscal_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["affiliate_payout_batches.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_affiliate_payout_requests_batch_id", "affiliate_payout_requests", ["batch_id"])
    op.create_index("ix_affiliate_payout_requests_participant_id", "affiliate_payout_requests", ["participant_id"])
    op.create_index("ix_affiliate_payout_requests_provider_transfer_id", "affiliate_payout_requests", ["provider_transfer_id"])
    op.create_index("ix_affiliate_payout_requests_status", "affiliate_payout_requests", ["status"])

    op.create_table(
        "affiliate_ledger_entries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("reward_id", UUID, nullable=True),
        sa.Column("payout_request_id", UUID, nullable=True),
        sa.Column("entry_type", sa.String(48), nullable=False),
        sa.Column("account", sa.String(16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="BRL"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "account IN ('pending', 'available', 'reserved', 'credit', 'cash')",
            name="ck_affiliate_ledger_account",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["reward_id"], ["affiliate_rewards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["payout_request_id"], ["affiliate_payout_requests.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_affiliate_ledger_entries_account", "affiliate_ledger_entries", ["account"])
    op.create_index("ix_affiliate_ledger_entries_created_at", "affiliate_ledger_entries", ["created_at"])
    op.create_index("ix_affiliate_ledger_entries_entry_type", "affiliate_ledger_entries", ["entry_type"])
    op.create_index("ix_affiliate_ledger_entries_participant_id", "affiliate_ledger_entries", ["participant_id"])

    op.create_table(
        "affiliate_magic_links",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("participant_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_affiliate_magic_links_participant_id", "affiliate_magic_links", ["participant_id"])
    op.create_index("ix_affiliate_magic_links_token_hash", "affiliate_magic_links", ["token_hash"])

    customer_policy_id = uuid.uuid4()
    partner_policy_id = uuid.uuid4()
    policies = sa.table(
        "affiliate_policies",
        sa.column("id", UUID),
        sa.column("mode", sa.String),
        sa.column("version", sa.Integer),
        sa.column("status", sa.String),
        sa.column("terms_version", sa.String),
        sa.column("referral_discount_bps", sa.Integer),
        sa.column("commission_bps", sa.Integer),
        sa.column("customer_reward_monthly_cents", sa.Integer),
        sa.column("customer_reward_quarterly_cents", sa.Integer),
        sa.column("customer_reward_yearly_cents", sa.Integer),
        sa.column("attribution_window_days", sa.Integer),
        sa.column("cooling_off_days", sa.Integer),
        sa.column("payout_minimum_cents", sa.Integer),
        sa.column("effective_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        policies,
        [
            {
                "id": customer_policy_id,
                "mode": "customer",
                "version": 1,
                "status": "active",
                "terms_version": "customer-v1-2026-09",
                "referral_discount_bps": 1000,
                "commission_bps": 0,
                "customer_reward_monthly_cents": 2000,
                "customer_reward_quarterly_cents": 5000,
                "customer_reward_yearly_cents": 15000,
                "attribution_window_days": 30,
                "cooling_off_days": 14,
                "payout_minimum_cents": 10000,
                "effective_at": datetime.now(UTC),
            },
            {
                "id": partner_policy_id,
                "mode": "partner",
                "version": 1,
                "status": "active",
                "terms_version": "partner-v1-2026-09",
                "referral_discount_bps": 1500,
                "commission_bps": 2000,
                "customer_reward_monthly_cents": 2000,
                "customer_reward_quarterly_cents": 5000,
                "customer_reward_yearly_cents": 15000,
                "attribution_window_days": 30,
                "cooling_off_days": 14,
                "payout_minimum_cents": 10000,
                "effective_at": datetime.now(UTC),
            },
        ],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO feature_flags (key, description, enabled_global, audience, created_at, updated_at)
            VALUES
              ('affiliate_customer_program', 'Programa de indicação para clientes', false, NULL, now(), now()),
              ('affiliate_partner_program', 'Piloto de afiliados parceiros', false, NULL, now(), now()),
              ('affiliate_cash_payouts', 'Saques Pix do programa de afiliados', false, NULL, now(), now())
            ON CONFLICT (key) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM feature_flags WHERE key IN "
            "('affiliate_customer_program', 'affiliate_partner_program', 'affiliate_cash_payouts')"
        )
    )
    op.drop_table("affiliate_magic_links")
    op.drop_table("affiliate_ledger_entries")
    op.drop_table("affiliate_payout_requests")
    op.drop_table("affiliate_payout_batches")
    op.drop_table("affiliate_fiscal_profiles")
    op.drop_table("affiliate_rewards")
    op.drop_table("affiliate_referrals")
    op.drop_table("affiliate_codes")
    op.drop_table("affiliate_participants")
    op.drop_table("affiliate_policies")
