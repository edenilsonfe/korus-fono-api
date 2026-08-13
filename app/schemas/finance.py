"""Wire contracts for internal clinic finance (camelCase JSON)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import CamelModel

CategoryKind = Literal["income", "expense"]
BillingMode = Literal["individual", "package", "courtesy"]


class FinancialProfileUpdate(CamelModel):
    person_type: Literal["PF", "PJ"] = "PF"
    legal_name: str = Field(default="", max_length=180)
    trade_name: str = Field(default="", max_length=180)
    document: str = Field(default="", max_length=20)
    council_registration: str = Field(default="", max_length=80)
    municipal_registration: str = Field(default="", max_length=80)
    address_line: str = Field(default="", max_length=255)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=2)
    postal_code: str = Field(default="", max_length=12)


class FinancialProfileResponse(FinancialProfileUpdate):
    id: UUID
    active: bool


class NamedConfigCreate(CamelModel):
    name: str = Field(min_length=1, max_length=100)
    kind: CategoryKind | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()


class NamedConfigUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    active: bool | None = None


class NamedConfigResponse(CamelModel):
    id: UUID
    name: str
    kind: CategoryKind | None = None
    active: bool


class ServiceCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    duration: int = Field(default=50, ge=1, le=1440)
    price_cents: int = Field(ge=1, le=1_000_000_000)
    category_id: UUID | None = None


class ServiceUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    duration: int | None = Field(default=None, ge=1, le=1440)
    price_cents: int | None = Field(default=None, ge=1, le=1_000_000_000)
    category_id: UUID | None = None
    active: bool | None = None


class ServiceResponse(ServiceCreate):
    id: UUID
    active: bool


class PackageCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    service_id: UUID | None = None
    sessions_count: int = Field(ge=1, le=1000)
    price_cents: int = Field(ge=1, le=1_000_000_000)
    validity_days: int = Field(default=30, ge=1, le=3650)


class PackageUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    service_id: UUID | None = None
    sessions_count: int | None = Field(default=None, ge=1, le=1000)
    price_cents: int | None = Field(default=None, ge=1, le=1_000_000_000)
    validity_days: int | None = Field(default=None, ge=1, le=3650)
    active: bool | None = None


class PackageResponse(PackageCreate):
    id: UUID
    active: bool


class ReceivableItemCreate(CamelModel):
    description: str = Field(min_length=1, max_length=255)
    quantity: int = Field(default=1, ge=1, le=10_000)
    unit_cents: int = Field(ge=1, le=1_000_000_000)
    service_id: UUID | None = None
    item_type: Literal["service", "package", "cancellation_fee", "other"] = "service"


class ReceivableCreate(CamelModel):
    patient_id: UUID | None = None
    payer_name: str = Field(min_length=1, max_length=180)
    payer_document: str = Field(default="", max_length=20)
    description: str = Field(min_length=1, max_length=255)
    issue_date: date = Field(default_factory=date.today)
    competence_date: date | None = None
    due_date: date
    category_id: UUID | None = None
    notes: str = Field(default="", max_length=4000)
    origin: Literal["manual", "appointment", "package", "cancellation_fee"] = "manual"
    items: list[ReceivableItemCreate] = Field(min_length=1, max_length=100)


class ReceivableItemResponse(CamelModel):
    id: UUID
    service_id: UUID | None
    appointment_id: UUID | None
    item_type: str
    description: str
    quantity: int
    unit_cents: int
    total_cents: int


class ReceivableResponse(CamelModel):
    id: UUID
    patient_id: UUID | None
    patient_name: str
    payer_name: str
    payer_document: str
    description: str
    issue_date: date
    competence_date: date
    due_date: date
    category_id: UUID | None
    total_cents: int
    paid_cents: int
    balance_cents: int
    status: str
    overdue: bool
    origin: str
    notes: str
    items: list[ReceivableItemResponse] = Field(default_factory=list)


class ReceivableListResponse(CamelModel):
    items: list[ReceivableResponse]
    total: int


class PaymentAllocationCreate(CamelModel):
    receivable_id: UUID
    amount_cents: int = Field(ge=1, le=1_000_000_000)


class PaymentCreate(CamelModel):
    patient_id: UUID | None = None
    payer_name: str = Field(min_length=1, max_length=180)
    payer_document: str = Field(default="", max_length=20)
    payment_date: date
    amount_cents: int = Field(ge=1, le=1_000_000_000)
    method_id: UUID | None = None
    notes: str = Field(default="", max_length=4000)
    allocations: list[PaymentAllocationCreate] = Field(min_length=1, max_length=100)


class PaymentAllocationResponse(CamelModel):
    receivable_id: UUID
    amount_cents: int


class PaymentResponse(CamelModel):
    id: UUID
    patient_id: UUID | None
    patient_name: str
    payer_name: str
    payer_document: str
    payment_date: date
    amount_cents: int
    method_id: UUID | None
    status: str
    receipt_number: str
    notes: str
    allocations: list[PaymentAllocationResponse]


class PayableCreate(CamelModel):
    description: str = Field(min_length=1, max_length=255)
    supplier_name: str = Field(default="", max_length=180)
    issue_date: date = Field(default_factory=date.today)
    competence_date: date | None = None
    due_date: date
    total_cents: int = Field(ge=1, le=1_000_000_000)
    category_id: UUID | None = None
    recurrence: Literal["monthly", "weekly", "yearly"] | None = None
    notes: str = Field(default="", max_length=4000)


class SettlementCreate(CamelModel):
    payment_date: date
    amount_cents: int = Field(ge=1, le=1_000_000_000)
    method_id: UUID | None = None
    notes: str = Field(default="", max_length=4000)


class SettlementResponse(SettlementCreate):
    id: UUID
    payable_id: UUID
    status: str


class PayableResponse(CamelModel):
    id: UUID
    description: str
    supplier_name: str
    issue_date: date
    competence_date: date
    due_date: date
    total_cents: int
    paid_cents: int
    balance_cents: int
    category_id: UUID | None
    status: str
    overdue: bool
    recurrence: str | None
    notes: str
    settlements: list[SettlementResponse] = Field(default_factory=list)


class PayableListResponse(CamelModel):
    items: list[PayableResponse]
    total: int


class CancellationRequest(CamelModel):
    reason: str = Field(min_length=3, max_length=1000)


class PatientPackageCreate(CamelModel):
    patient_id: UUID
    package_id: UUID
    started_on: date
    due_date: date
    payer_name: str = Field(min_length=1, max_length=180)
    payer_document: str = Field(default="", max_length=20)


class PatientPackageResponse(CamelModel):
    id: UUID
    patient_id: UUID | None
    patient_name: str
    package_id: UUID | None
    package_name: str
    receivable_id: UUID | None
    started_on: date
    expires_on: date
    sessions_included: int
    sessions_used: int
    sessions_remaining: int
    agreed_price_cents: int
    status: str


class AppointmentCompleteRequest(CamelModel):
    billing_mode: BillingMode
    service_id: UUID | None = None
    patient_package_id: UUID | None = None
    due_date: date | None = None
    payer_name: str | None = Field(default=None, max_length=180)
    payer_document: str = Field(default="", max_length=20)
    notes: str = Field(default="", max_length=4000)


class AppointmentCompleteResponse(CamelModel):
    appointment_id: UUID
    session_id: UUID
    receivable_id: UUID | None
    package_usage_id: UUID | None
    billing_mode: BillingMode


class PatientFinanceResponse(CamelModel):
    patient_id: UUID
    receivables: list[ReceivableResponse]
    payments: list[PaymentResponse]
    packages: list[PatientPackageResponse]
    open_balance_cents: int


class CashFlowResponse(CamelModel):
    from_date: date
    to_date: date
    realized_income_cents: int
    realized_expense_cents: int
    projected_income_cents: int
    projected_expense_cents: int
    realized_balance_cents: int
    projected_balance_cents: int


class FinanceDashboardResponse(CamelModel):
    open_receivables_cents: int
    overdue_receivables_cents: int
    received_this_month_cents: int
    payable_this_month_cents: int
    paid_this_month_cents: int
    net_cash_this_month_cents: int
    overdue_count: int


class AuditEventResponse(CamelModel):
    id: UUID
    entity_type: str
    entity_id: UUID
    action: str
    payload: dict
    created_at: datetime
