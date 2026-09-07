"""Affiliate program persistence.

The ledger is append-only. Balances are projections of ledger entries and must
never be stored as mutable counters.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_uuid


class AffiliatePolicy(Base, TimestampMixin):
    __tablename__ = "affiliate_policies"
    __table_args__ = (
        UniqueConstraint("mode", "version", name="uq_affiliate_policy_mode_version"),
        CheckConstraint(
            "mode IN ('customer', 'partner')", name="ck_affiliate_policy_mode"
        ),
        CheckConstraint(
            "status IN ('draft', 'scheduled', 'active', 'retired')",
            name="ck_affiliate_policy_status",
        ),
        Index(
            "uq_affiliate_policy_active_mode",
            "mode",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", index=True
    )
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    referral_discount_bps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    commission_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    customer_reward_monthly_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    customer_reward_quarterly_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    customer_reward_yearly_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    attribution_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    cooling_off_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    payout_minimum_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10000
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
    )

    def snapshot(self, *, commission_bps: int | None = None) -> dict:
        return {
            "mode": self.mode,
            "version": self.version,
            "termsVersion": self.terms_version,
            "referralDiscountBps": self.referral_discount_bps,
            "commissionBps": self.commission_bps
            if commission_bps is None
            else commission_bps,
            "customerRewardMonthlyCents": self.customer_reward_monthly_cents,
            "customerRewardQuarterlyCents": self.customer_reward_quarterly_cents,
            "customerRewardYearlyCents": self.customer_reward_yearly_cents,
            "attributionWindowDays": self.attribution_window_days,
            "coolingOffDays": self.cooling_off_days,
            "payoutMinimumCents": self.payout_minimum_cents,
        }


class AffiliateParticipant(Base, TimestampMixin):
    __tablename__ = "affiliate_participants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'deactivated', 'closed')",
            name="ck_affiliate_participant_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    email: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    public_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="invited", index=True
    )
    customer_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    partner_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    customer_terms_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    customer_terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    partner_terms_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partner_terms_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    commission_override_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    codes: Mapped[list["AffiliateCode"]] = relationship(back_populates="participant")


class AffiliateCode(Base, TimestampMixin):
    __tablename__ = "affiliate_codes"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('customer', 'partner')", name="ck_affiliate_code_mode"
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_affiliate_code_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code: Mapped[str] = mapped_column(
        String(48), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    participant: Mapped[AffiliateParticipant] = relationship(back_populates="codes")


class AffiliateReferral(Base, TimestampMixin):
    __tablename__ = "affiliate_referrals"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('customer', 'partner')", name="ck_affiliate_referral_mode"
        ),
        CheckConstraint(
            "status IN ('registered', 'converted', 'rejected')",
            name="ck_affiliate_referral_status",
        ),
        CheckConstraint(
            "review_state IN ('clear', 'manual_review', 'approved', 'rejected')",
            name="ck_affiliate_referral_review_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_codes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    referred_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="registered", index=True
    )
    review_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="clear", index=True
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    benefit_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    converted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AffiliateReward(Base, TimestampMixin):
    __tablename__ = "affiliate_rewards"
    __table_args__ = (
        UniqueConstraint(
            "referral_id",
            "source_payment_id",
            "kind",
            name="uq_affiliate_reward_source",
        ),
        CheckConstraint(
            "state IN ('pending', 'coolingOff', 'available', 'reserved', 'credited', "
            "'paid', 'reversed', 'voided')",
            name="ck_affiliate_reward_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    referral_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_referrals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_payment_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    external_revenue_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BRL")
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reversed_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AffiliateLedgerEntry(Base):
    __tablename__ = "affiliate_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "account IN ('pending', 'available', 'reserved', 'credit', 'cash')",
            name="ck_affiliate_ledger_account",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reward_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_rewards.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payout_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_payout_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    entry_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    account: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BRL")
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class AffiliateFiscalProfile(Base, TimestampMixin):
    __tablename__ = "affiliate_fiscal_profiles"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "version", name="uq_affiliate_fiscal_version"
        ),
        CheckConstraint(
            "person_type IN ('pf', 'pj')", name="ck_affiliate_fiscal_person_type"
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')",
            name="ck_affiliate_fiscal_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    person_type: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    document_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    document_masked: Mapped[str] = mapped_column(String(24), nullable=False)
    encrypted_document: Mapped[str] = mapped_column(Text, nullable=False)
    pix_key_type: Mapped[str] = mapped_column(String(16), nullable=False)
    pix_key_masked: Mapped[str] = mapped_column(String(255), nullable=False)
    pix_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_pix_key: Mapped[str] = mapped_column(Text, nullable=False)
    pix_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawal_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
    )


class AffiliatePayoutBatch(Base, TimestampMixin):
    __tablename__ = "affiliate_payout_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'processing', 'paid', 'failed')",
            name="ck_affiliate_payout_batch_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    competence: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", index=True
    )
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prepared_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AffiliatePayoutRequest(Base, TimestampMixin):
    __tablename__ = "affiliate_payout_requests"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "request_id", name="uq_affiliate_payout_request_id"
        ),
        CheckConstraint(
            "status IN ('requested', 'batched', 'approved', 'processing', 'paid', 'failed', 'canceled')",
            name="ck_affiliate_payout_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fiscal_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_fiscal_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_payout_batches.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="requested", index=True
    )
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    withholding_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    net_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_transfer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cancellable_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AffiliateCreditCheckout(Base, TimestampMixin):
    __tablename__ = "affiliate_credit_checkouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'released', 'settled', 'refunded')",
            name="ck_affiliate_credit_checkout_state",
        ),
        CheckConstraint(
            "amount_cents >= 0 AND refunded_cents >= 0 AND refunded_cents <= amount_cents",
            name="ck_affiliate_credit_checkout_amount",
        ),
        UniqueConstraint(
            "reservation_id", "attempt", name="uq_affiliate_credit_checkout_attempt"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reservation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    charge_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    source_payment_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )


class AffiliateMagicLink(Base):
    __tablename__ = "affiliate_magic_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
