from pydantic import EmailStr, Field, field_validator

from app.core.specialty_catalog import is_valid_specialty_key
from app.schemas.common import CamelModel
from app.services.auth_rate_limit import normalize_auth_email


def _normalize_cpf(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


class RegisterRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str
    specialty_key: str
    council: str = ""
    phone: str
    cpf: str | None = None
    referral_code: str | None = Field(default=None, max_length=48)

    @field_validator("referral_code")
    @classmethod
    def normalize_referral_code(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        return normalized or None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalize_auth_email(value)

    @field_validator("specialty_key")
    @classmethod
    def validate_specialty_key(cls, value: str) -> str:
        if not is_valid_specialty_key(value):
            raise ValueError("Especialidade inválida")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        phone = str(value or "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit()).lstrip("0")
        national = digits[2:] if digits.startswith("55") else digits
        if len(national) not in (10, 11):
            raise ValueError("Telefone deve conter DDD + número")
        if not 11 <= int(national[:2]) <= 99:
            raise ValueError("DDD inválido")
        if len(national) == 11 and not national[2:].startswith("9"):
            raise ValueError("Celular inválido: o número deve começar com 9 após o DDD")
        return phone

    @field_validator("cpf")
    @classmethod
    def validate_cpf(cls, value: str | None) -> str:
        if value is None or not str(value).strip():
            return ""
        digits = _normalize_cpf(value)
        if len(digits) != 11:
            raise ValueError("CPF deve conter 11 dígitos")
        if digits == digits[0] * 11:
            raise ValueError("CPF inválido")
        return digits


class LoginRequest(CamelModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalize_auth_email(value)


class RefreshRequest(CamelModel):
    refresh_token: str = ""


class TokenResponse(CamelModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(CamelModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalize_auth_email(value)


class ResetPasswordRequest(CamelModel):
    token: str
    new_password: str = Field(min_length=8)


class ChangePasswordRequest(CamelModel):
    current_password: str
    new_password: str = Field(min_length=8)


class VerifyEmailRequest(CamelModel):
    token: str


class MessageResponse(CamelModel):
    message: str
