"""Public billing plan and checkout schemas."""

from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


def _digits_only(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def _is_valid_cpf(digits: str) -> bool:
    if len(digits) != 11 or digits == digits[0] * 11:
        return False

    def check_digit(length: int) -> int:
        total = sum(int(digits[index]) * (length + 1 - index) for index in range(length))
        remainder = (total * 10) % 11
        return 0 if remainder == 10 else remainder

    return check_digit(9) == int(digits[9]) and check_digit(10) == int(digits[10])


def _is_valid_cnpj(digits: str) -> bool:
    if len(digits) != 14 or digits == digits[0] * 14:
        return False

    def check_digit(base: str, weights: tuple[int, ...]) -> int:
        remainder = sum(int(number) * weight for number, weight in zip(base, weights)) % 11
        return 0 if remainder < 2 else 11 - remainder

    first = check_digit(digits[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = check_digit(
        f"{digits[:12]}{first}",
        (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    )
    return digits[-2:] == f"{first}{second}"


class PlanPublicResponse(CamelModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    limits: dict[str, Any] | None = None
    price_cents: int = 0
    currency: str = "BRL"
    billing_interval: str = "monthly"
    features: list[Any] = Field(default_factory=list)
    badge: str | None = None
    highlighted: bool = False
    display_order: int = 0
    is_active: bool = True


class CheckoutRequest(CamelModel):
    plan_slug: str
    billing_document_type: Literal["cpf", "cnpj"] | None = None
    cnpj: str | None = None
    # Compatibilidade temporária com a primeira versão do contrato CPF/CNPJ.
    billing_document: str | None = None
    # Compatibilidade com o contrato anterior, que enviava somente cpf.
    cpf: str | None = None
    coupon_code: str | None = None

    @field_validator("cpf")
    @classmethod
    def validate_cpf(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = _digits_only(value)
        if not _is_valid_cpf(digits):
            raise ValueError("CPF inválido")
        return digits

    @field_validator("cnpj")
    @classmethod
    def validate_cnpj(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = _digits_only(value)
        if not _is_valid_cnpj(digits):
            raise ValueError("CNPJ inválido")
        return digits

    @field_validator("billing_document")
    @classmethod
    def validate_legacy_billing_document(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = _digits_only(value)
        if not (_is_valid_cpf(digits) or _is_valid_cnpj(digits)):
            raise ValueError("CPF ou CNPJ inválido")
        return digits


class CheckoutResponse(CamelModel):
    checkout_url: str | None = None
    session_id: str | None = None
    status: str | None = None
    provider: str | None = None
    message: str | None = None
    change_type: str | None = None
    charge_cents: int | None = None
    credit_cents: int | None = None
    scheduled_at: str | None = None


class PlanChangePreviewResponse(CamelModel):
    change_type: str
    message: str
    current_plan_slug: str | None = None
    target_plan_slug: str | None = None
    credit_cents: int | None = None
    charge_cents: int | None = None
    target_price_cents: int | None = None
    scheduled_at: str | None = None
    period_end: str | None = None
    remaining_days: int | None = None


class ReconcileResponse(CamelModel):
    applied: bool
    message: str
    payments_checked: int = 0
    subscription_status: str | None = None
    professional_status: str | None = None
    plan_slug: str | None = None


class PlanSummary(CamelModel):
    id: str
    slug: str
    name: str
    billing_interval: str = "monthly"


class PendingPlanSummary(CamelModel):
    id: str
    slug: str
    name: str
    billing_interval: str = "monthly"


class SubscriptionSummary(CamelModel):
    id: str
    status: str
    plan: PlanSummary
    started_at: str | None = None
    last_payment_at: str | None = None
    current_period_end: str | None = None
    pending_plan: PendingPlanSummary | None = None
    pending_change_at: str | None = None


class BillingMeResponse(CamelModel):
    subscription_status: str
    billing_cpf: str = ""
    billing_cnpj: str = ""
    billing_document_type: Literal["cpf", "cnpj"] = "cpf"
    # Mantido durante a transição: documento correspondente ao tipo selecionado.
    billing_document: str = ""
    trial_started_at: str | None = None
    trial_ends_at: str | None = None
    can_write: bool
    subscription: SubscriptionSummary | None = None


class PaymentSessionPlan(CamelModel):
    slug: str
    name: str
    description: str | None = None
    price_cents: int
    currency: str = "BRL"
    billing_interval: str = "monthly"


class PaymentSessionResponse(CamelModel):
    session_id: str
    provider: str
    status: str
    plan: PaymentSessionPlan
    customer_name: str
    customer_email: str
    has_billing_document: bool = False
    billing_document_type: Literal["cpf", "cnpj"] | None = None
    # Mantido para clientes antigos; indica especificamente um CPF de 11 dígitos.
    has_cpf: bool = False
    charge_cents: int | None = None
    change_type: str | None = None
    credit_cents: int | None = None
    # Fatura Asaas (cartão fora do nosso origin) — null no stub / se indisponível
    invoice_url: str | None = None


class PixCheckoutResponse(CamelModel):
    session_id: str
    provider: str
    encoded_image: str | None = None
    payload: str | None = None
    expiration_date: str | None = None


class CardInvoiceResponse(CamelModel):
    session_id: str
    invoice_url: str
