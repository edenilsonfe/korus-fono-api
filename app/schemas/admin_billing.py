from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import CamelModel, PaginatedResponse

CouponType = Literal["percent", "fixed_cents"]


class AdminSubscriptionListItem(CamelModel):
    id: str
    professional_id: str
    professional_name: str
    professional_email: str
    plan_slug: str | None = None
    plan_name: str | None = None
    status: str
    provider: str | None = None
    external_subscription_id: str | None = None
    external_checkout_id: str | None = None
    updated_at: datetime


class AdminSubscriptionsPage(PaginatedResponse[AdminSubscriptionListItem]):
    pass


class AdminBillingEventItem(CamelModel):
    id: str
    provider: str
    external_event_id: str
    event_type: str
    status: str
    created_at: datetime
    processed_at: datetime | None = None


class AdminSubscriptionDetail(AdminSubscriptionListItem):
    professional_subscription_status: str
    started_at: datetime | None = None
    last_payment_at: datetime | None = None
    current_period_end: datetime | None = None
    recent_events: list[AdminBillingEventItem] = Field(default_factory=list)


class AdminPlanItem(CamelModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    price_cents: int
    currency: str
    billing_interval: str
    features: list[Any] = Field(default_factory=list)
    badge: str | None = None
    highlighted: bool = False
    display_order: int = 0
    is_active: bool = True


class AdminPlanCreate(CamelModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    description: str | None = None
    price_cents: int = Field(ge=0)
    currency: str = "BRL"
    billing_interval: str = "monthly"
    features: list[Any] = Field(default_factory=list)
    badge: str | None = None
    highlighted: bool = False
    display_order: int = 0
    is_active: bool = True
    reason: str | None = None


class AdminPlanUpdate(CamelModel):
    name: str | None = None
    description: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    billing_interval: str | None = None
    features: list[Any] | None = None
    badge: str | None = None
    highlighted: bool | None = None
    display_order: int | None = None
    is_active: bool | None = None
    reason: str | None = None


class CouponItem(CamelModel):
    id: str
    code: str
    coupon_type: str
    value: int
    trial_bonus_days: int = 0
    valid_from: date | None = None
    valid_until: date | None = None
    max_redemptions: int | None = None
    max_per_professional: int = 1
    plan_slugs: list[str] | None = None
    is_active: bool = True
    external_coupon_id: str | None = None
    redemption_count: int = 0
    reserved_count: int = 0
    has_history: bool = False


class CouponCreate(CamelModel):
    code: str = Field(min_length=2, max_length=64)
    coupon_type: CouponType
    value: int = Field(gt=0)
    trial_bonus_days: int = Field(default=0, ge=0, le=365)
    valid_from: date | None = None
    valid_until: date | None = None
    max_redemptions: int | None = Field(default=None, ge=1)
    max_per_professional: int = Field(default=1, ge=1, le=1)
    plan_slugs: list[str] | None = None
    is_active: bool = True
    reason: str | None = None


class CouponUpdate(CamelModel):
    code: str | None = Field(default=None, min_length=2, max_length=64)
    coupon_type: CouponType | None = None
    value: int | None = Field(default=None, gt=0)
    trial_bonus_days: int | None = Field(default=None, ge=0, le=365)
    valid_from: date | None = None
    valid_until: date | None = None
    max_redemptions: int | None = Field(default=None, ge=1)
    max_per_professional: int | None = Field(default=None, ge=1, le=1)
    plan_slugs: list[str] | None = None
    is_active: bool | None = None
    reason: str | None = None


class ApplyCouponBody(CamelModel):
    code: str
    reason: str | None = None


class ApplyCouponResult(CamelModel):
    coupon_code: str
    trial_extended_days: int = 0
    message: str


class BillingMetrics(CamelModel):
    period_days: int
    mrr_cents: int
    churn_count: int
    new_trials: int
    conversions: int
    trial_expired: int


class ReconcileAdminResult(CamelModel):
    applied: bool
    message: str
    payments_checked: int = 0
    subscription_status: str | None = None
    professional_status: str | None = None
    plan_slug: str | None = None
