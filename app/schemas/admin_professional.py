from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.admin_operations import AdminRole
from app.schemas.common import CamelModel, PaginatedResponse

SubscriptionStatus = Literal["trialing", "active", "trial_expired", "past_due", "canceled"]
SaasPaymentMethod = Literal["pix", "credit_card"]


class AdminPlanSummary(CamelModel):
    slug: str | None = None
    name: str | None = None
    status: str | None = None
    billing_interval: str | None = None


class AdminProfessionalListItem(CamelModel):
    id: str
    name: str
    email: str
    specialty_key: str
    subscription_status: str
    trial_ends_at: datetime | None = None
    is_staff: bool
    admin_role: AdminRole | None = None
    is_disabled: bool
    created_at: datetime
    plan: AdminPlanSummary | None = None
    payment_method: SaasPaymentMethod | None = None


class AdminProfessionalCounts(CamelModel):
    patients: int = 0
    sessions: int = 0
    assessments: int = 0


class AdminWhatsAppSummary(CamelModel):
    status: str | None = None
    updated_at: datetime | None = None


class AdminAIJobSummary(CamelModel):
    id: str
    job_type: str
    status: str
    created_at: datetime


class AdminProfessionalDetail(CamelModel):
    id: str
    name: str
    email: str
    phone: str
    cpf_masked: str
    specialty: str
    specialty_key: str
    council: str
    is_staff: bool
    admin_role: AdminRole | None = None
    is_disabled: bool
    subscription_status: str
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    plan: AdminPlanSummary | None = None
    counts: AdminProfessionalCounts
    whatsapp: AdminWhatsAppSummary
    recent_ai_jobs: list[AdminAIJobSummary] = Field(default_factory=list)


class AdminProfessionalsPage(PaginatedResponse[AdminProfessionalListItem]):
    pass


class AdminHubStats(CamelModel):
    trialing: int = 0
    trial_expired: int = 0
    active: int = 0
    staff: int = 0
    disabled: int = 0


class AdminReasonBody(CamelModel):
    reason: str = Field(min_length=5, max_length=500)


class ExtendTrialBody(CamelModel):
    days: int = Field(default=7, ge=1, le=365)
    reason: str = Field(min_length=5, max_length=500)


class SetStaffBody(CamelModel):
    is_staff: bool
    reason: str = Field(min_length=5, max_length=500)


class SetSubscriptionStatusBody(CamelModel):
    status: SubscriptionStatus
    reason: str = Field(min_length=5, max_length=500)
