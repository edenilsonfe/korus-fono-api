"""DTOs do canal de suporte (form de contato)."""

from pydantic import Field

from app.schemas.common import CamelModel


class SupportContactRequest(CamelModel):
    """Form de contato enviado da página /suporte. Remetente vem da sessão."""

    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=10, max_length=4000)


class SupportContactResponse(CamelModel):
    message: str
