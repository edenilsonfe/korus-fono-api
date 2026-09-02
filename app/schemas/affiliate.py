from datetime import datetime
from uuid import UUID

from pydantic import EmailStr, Field, field_validator

from app.schemas.common import CamelModel


class AffiliateBalances(CamelModel):
    pending: int = 0
    available: int = 0
    reserved: int = 0
    credit: int = 0
    cash: int = 0


class AffiliateParticipantSummary(CamelModel):
    id: UUID
    status: str
    customer_enabled: bool
    partner_enabled: bool
    public_name: str | None = None


class AffiliateReferralSummary(CamelModel):
    id: UUID
    mode: str
    status: str
    review_state: str
    created_at: datetime
    converted_at: datetime | None = None


class AffiliateRewardSummary(CamelModel):
    id: UUID
    kind: str
    state: str
    gross_cents: int
    available_at: datetime | None = None
    created_at: datetime


class AffiliateDashboardResponse(CamelModel):
    eligible: bool
    terms_version: str
    participant: AffiliateParticipantSummary | None = None
    code: str | None = None
    balances: AffiliateBalances
    referrals: list[AffiliateReferralSummary]
    rewards: list[AffiliateRewardSummary]


class AffiliateOptInBody(CamelModel):
    terms_version: str = Field(min_length=1, max_length=64)


class AffiliateOptInResponse(CamelModel):
    participant_id: UUID
    mode: str
    code: str


class PublicAffiliateCodeResponse(CamelModel):
    code: str
    mode: str
    benefit_percent: int
    public_name: str | None = None
    expires_in_days: int


class AdminAffiliateOverview(CamelModel):
    active_policies: int
    participants: int
    active_participants: int
    referrals: int
    pending_reviews: int
    rewards: int
    available_cents: int


class AdminAffiliatePolicyCreate(CamelModel):
    mode: str
    terms_version: str = Field(min_length=1, max_length=64)
    referral_discount_bps: int = Field(ge=0, le=10000)
    commission_bps: int = Field(ge=0, le=10000)
    customer_reward_monthly_cents: int = Field(ge=0)
    customer_reward_quarterly_cents: int = Field(ge=0)
    customer_reward_yearly_cents: int = Field(ge=0)
    attribution_window_days: int = Field(30, ge=1, le=365)
    cooling_off_days: int = Field(14, ge=0, le=90)
    payout_minimum_cents: int = Field(10000, ge=0)
    effective_at: datetime
    activate: bool = False

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"customer", "partner"}:
            raise ValueError("Modo inválido")
        return value


class AdminAffiliatePolicyItem(CamelModel):
    id: UUID
    mode: str
    version: int
    status: str
    terms_version: str
    referral_discount_bps: int
    commission_bps: int
    customer_reward_monthly_cents: int
    customer_reward_quarterly_cents: int
    customer_reward_yearly_cents: int
    attribution_window_days: int
    cooling_off_days: int
    payout_minimum_cents: int
    effective_at: datetime
    created_at: datetime


class AdminAffiliateParticipantItem(CamelModel):
    id: UUID
    email: EmailStr
    public_name: str | None = None
    status: str
    customer_enabled: bool
    partner_enabled: bool
    commission_override_bps: int | None = None
    balances: AffiliateBalances
    created_at: datetime


class AdminAffiliatePartnerInvite(CamelModel):
    email: EmailStr
    public_name: str = Field(min_length=2, max_length=120)
    commission_override_bps: int | None = Field(None, ge=0, le=10000)


class AdminAffiliateParticipantStatusBody(CamelModel):
    status: str
    reason: str = Field(min_length=3, max_length=500)


class AdminAffiliateReviewBody(CamelModel):
    decision: str
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        if value not in {"approved", "rejected"}:
            raise ValueError("Decisão inválida")
        return value


class AdminAffiliateCorrectionBody(CamelModel):
    participant_id: UUID
    account: str
    amount_cents: int
    reason: str = Field(min_length=3, max_length=500)
    evidence_reference: str = Field(min_length=1, max_length=255)

    @field_validator("amount_cents")
    @classmethod
    def validate_nonzero_amount(cls, value: int) -> int:
        if value == 0:
            raise ValueError("O valor da correção não pode ser zero")
        return value

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        if value not in {"pending", "available", "reserved"}:
            raise ValueError("Conta contábil inválida")
        return value


class AdminAffiliateCorrectionResult(CamelModel):
    id: UUID
    participant_id: UUID
    account: str
    amount_cents: int
    created_at: datetime


class AffiliateCreditRedemptionBody(CamelModel):
    amount_cents: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=80)


class AffiliateCreditRedemptionResult(CamelModel):
    converted_cents: int
    credit_balance_cents: int


class AffiliateFiscalProfileBody(CamelModel):
    person_type: str
    legal_name: str = Field(min_length=2, max_length=255)
    document: str = Field(min_length=11, max_length=18)
    pix_key_type: str = Field(min_length=2, max_length=16)
    pix_key: str = Field(min_length=3, max_length=255)


class AffiliateFiscalProfileItem(CamelModel):
    id: UUID
    participant_id: UUID
    version: int
    person_type: str
    status: str
    legal_name: str
    document_masked: str
    pix_key_type: str
    pix_key_masked: str
    pix_validated_at: datetime | None = None
    withdrawal_locked_until: datetime | None = None
    created_at: datetime


class AffiliatePayoutRequestBody(CamelModel):
    amount_cents: int = Field(ge=10000)


class AffiliatePayoutItem(CamelModel):
    id: UUID
    participant_id: UUID
    fiscal_profile_id: UUID
    batch_id: UUID | None = None
    status: str
    gross_cents: int
    withholding_cents: int
    fee_cents: int
    net_cents: int
    provider_transfer_id: str | None = None
    requested_at: datetime
    cancellable_until: datetime
    processed_at: datetime | None = None
    failure_reason: str | None = None
    created_at: datetime


class AffiliatePayoutBatchItem(CamelModel):
    id: UUID
    competence: str
    status: str
    cutoff_at: datetime
    prepared_by_id: UUID
    approved_by_id: UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime


class AdminAffiliateFiscalApprovalBody(CamelModel):
    pix_validated: bool


class AdminAffiliateTransferBody(CamelModel):
    provider_transfer_id: str = Field(min_length=3, max_length=255)


class AffiliatePortalLinkRequest(CamelModel):
    email: EmailStr


class AffiliatePortalExchangeBody(CamelModel):
    token: str = Field(min_length=20, max_length=255)


class AffiliatePortalSession(CamelModel):
    participant_id: UUID
    public_name: str | None = None
    status: str
    partner_terms_version: str | None = None
    required_terms_version: str


class AffiliatePortalDashboard(CamelModel):
    participant: AffiliateParticipantSummary
    code: str | None = None
    balances: AffiliateBalances
    referrals: list[AffiliateReferralSummary]
    rewards: list[AffiliateRewardSummary]
