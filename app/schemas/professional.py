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
    avatar_color: str
    is_staff: bool = False
    email_verified: bool = False


class ProfessionalUpdate(CamelModel):
    name: str | None = None
    specialty_key: str | None = None
    council: str | None = None
    phone: str | None = None
    avatar_color: str | None = None

    @field_validator("specialty_key")
    @classmethod
    def validate_specialty_key(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_specialty_key(value):
            raise ValueError("Especialidade inválida")
        return value
