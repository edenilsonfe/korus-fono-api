import re
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.specialty_catalog import is_valid_specialty_key
from app.schemas.common import CamelModel


class ProfessionalResponse(CamelModel):
    id: str
    name: str
    specialty: str
    specialty_key: str
    council: str
    email: EmailStr
    phone: str
    cpf: str = ""
    billing_address: str = ""
    billing_address_number: str = ""
    billing_address_complement: str = ""
    billing_province: str = ""
    billing_postal_code: str = ""
    billing_profile_complete: bool = False
    avatar_color: str
    is_staff: bool = False
    admin_role: str | None = None
    admin_permissions: list[str] = Field(default_factory=list)
    email_verified: bool = False
    signup_payment_required: bool = False
    temporary_access_ends_at: datetime | None = None


class ProfessionalUpdate(CamelModel):
    name: str | None = None
    specialty_key: str | None = None
    council: str | None = None
    phone: str | None = None
    avatar_color: str | None = None
    billing_address: str | None = Field(default=None, max_length=255)
    billing_address_number: str | None = Field(default=None, max_length=30)
    billing_address_complement: str | None = Field(default=None, max_length=100)
    billing_province: str | None = Field(default=None, max_length=100)
    billing_postal_code: str | None = Field(default=None, max_length=12)

    @field_validator("specialty_key")
    @classmethod
    def validate_specialty_key(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_specialty_key(value):
            raise ValueError("Especialidade inválida")
        return value

    @field_validator(
        "billing_address",
        "billing_address_number",
        "billing_address_complement",
        "billing_province",
    )
    @classmethod
    def clean_billing_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("billing_postal_code")
    @classmethod
    def clean_billing_postal_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\D", "", value)[:8]
