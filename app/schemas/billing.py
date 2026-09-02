"""Public billing plan and checkout schemas."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator

from app.billing.constants import MAX_ANNUAL_CARD_INSTALLMENTS
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


def _is_valid_card_number(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


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
    access_granted: bool = False


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
    signup_payment_required: bool = False
    checkout_session_id: str | None = None
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
    billing_document: str = ""
    billing_postal_code: str = ""
    billing_address_number: str = ""
    billing_address_complement: str = ""
    billing_phone: str = ""
    # Mantido para clientes antigos; indica especificamente um CPF de 11 dígitos.
    has_cpf: bool = False
    charge_cents: int | None = None
    change_type: str | None = None
    credit_cents: int | None = None
    # Fatura Asaas (cartão fora do nosso origin) — null no stub / se indisponível
    invoice_url: str | None = None
    access_granted: bool = False


class PixCheckoutResponse(CamelModel):
    session_id: str
    provider: str
    encoded_image: str | None = None
    payload: str | None = None
    expiration_date: str | None = None


class CardInvoiceResponse(CamelModel):
    session_id: str
    invoice_url: str


class CreditCardPaymentRequest(CamelModel):
    """Transient card input. PAN and CVV must never be persisted or logged."""

    holder_name: str = Field(min_length=2, max_length=100)
    number: SecretStr
    expiry_month: str
    expiry_year: str
    ccv: SecretStr
    holder_email: EmailStr
    holder_document: str
    postal_code: str
    address_number: str = Field(min_length=1, max_length=30)
    address_complement: str | None = Field(default=None, max_length=100)
    phone: str
    installments: int = Field(default=1, ge=1, le=MAX_ANNUAL_CARD_INSTALLMENTS)

    @field_validator("holder_name", "address_number")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Campo obrigatório")
        return cleaned

    @field_validator("address_complement")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    @field_validator("number", mode="before")
    @classmethod
    def validate_number(cls, value: object) -> SecretStr:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value or "")
        digits = _digits_only(raw)
        if not _is_valid_card_number(digits):
            raise ValueError("Número do cartão inválido")
        return SecretStr(digits)

    @field_validator("ccv", mode="before")
    @classmethod
    def validate_ccv(cls, value: object) -> SecretStr:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value or "")
        digits = _digits_only(raw)
        if len(digits) not in (3, 4):
            raise ValueError("Código de segurança inválido")
        return SecretStr(digits)

    @field_validator("expiry_month")
    @classmethod
    def validate_expiry_month(cls, value: str) -> str:
        digits = _digits_only(value)
        if len(digits) not in (1, 2) or not 1 <= int(digits) <= 12:
            raise ValueError("Mês de validade inválido")
        return digits.zfill(2)

    @field_validator("expiry_year")
    @classmethod
    def validate_expiry_year(cls, value: str) -> str:
        digits = _digits_only(value)
        if len(digits) == 2:
            digits = f"20{digits}"
        current_year = datetime.now(UTC).year
        if len(digits) != 4 or not current_year <= int(digits) <= current_year + 20:
            raise ValueError("Ano de validade inválido")
        return digits

    @field_validator("holder_document")
    @classmethod
    def validate_holder_document(cls, value: str) -> str:
        digits = _digits_only(value)
        if not (_is_valid_cpf(digits) or _is_valid_cnpj(digits)):
            raise ValueError("CPF ou CNPJ do titular inválido")
        return digits

    @field_validator("postal_code")
    @classmethod
    def validate_postal_code(cls, value: str) -> str:
        digits = _digits_only(value)
        if len(digits) != 8:
            raise ValueError("CEP inválido")
        return digits

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        digits = _digits_only(value)
        if len(digits) not in (10, 11):
            raise ValueError("Telefone inválido")
        return digits

    @model_validator(mode="after")
    def validate_expiration(self) -> "CreditCardPaymentRequest":
        now = datetime.now(UTC)
        if int(self.expiry_year) == now.year and int(self.expiry_month) < now.month:
            raise ValueError("Cartão vencido")
        return self


class CreditCardPaymentResponse(CamelModel):
    session_id: str
    provider: str
    status: Literal["paid", "pending"]
    message: str
