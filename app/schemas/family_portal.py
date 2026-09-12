"""F14 — DTOs do portal da família (administração privada e raiz pública).

Payloads novos usam ``extra="forbid"``; JSON camelCase via ``CamelModel``.
A projeção pública (§3.4) é uma allowlist nova: nunca expõe IDs internos,
contatos, diagnóstico ou informação financeira.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from app.schemas.common import CamelModel

FamilyPortalGrantStatus = Literal["active", "expired", "revoked", "invalidated"]
RecipientWithdrawReason = Literal[
    "family_request", "incorrect_recipient", "professional_decision"
]

# Seções fixas da UI pública; não são permissões para consultar o prontuário.
PUBLIC_PORTAL_SECTIONS = (
    "session_summary",
    "goal",
    "material",
    "report",
    "notice",
)


class StrictCamelModel(CamelModel):
    """CamelCase com rejeição de campos desconhecidos (422)."""

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")


def _strip_required(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("O texto não pode ficar em branco")
    return value


# --------------------------------------------------------------------------- #
# Administração do portal (§3.1)
# --------------------------------------------------------------------------- #


class FamilyPortalResponse(CamelModel):
    """Representação do portal; ausência de linha é enabled=false, version=1."""

    id: str | None
    patient_id: str
    enabled: bool
    version: int
    recipient_count: int
    published_item_count: int


class FamilyPortalEnableRequest(StrictCamelModel):
    """Habilitação do portal; desabilitar é ação protetiva (``/disable``)."""

    enabled: Literal[True]
    expected_version: Annotated[int, Field(ge=1)]


class FamilyPortalDisableRequest(StrictCamelModel):
    """Corpo vazio; existe para rejeitar campos desconhecidos."""


class FamilyPortalAuthorizationInput(StrictCamelModel):
    """Declaração da profissional sobre a autorização do responsável.

    O software registra a declaração; não certifica a base legal. A referência
    é texto restrito (nunca arquivo/URL buscado pelo servidor).
    """

    authorized_at: datetime
    reference: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    reviewed: Literal[True]

    @field_validator("reference")
    @classmethod
    def strip_reference(cls, value: str | None) -> str | None:
        return _strip_required(value) if value is not None else None


class FamilyPortalRecipientUpsertRequest(StrictCamelModel):
    """Autorização explícita do destinatário.

    Novo destinatário: ``expectedVersion=null``. Já existente: ``expectedVersion``
    obrigatório e atual. Repetir o mesmo payload com versão obsoleta é 409.
    """

    expected_version: Annotated[int | None, Field(ge=1)] = None
    appointments_enabled: bool = False
    family_authorization: FamilyPortalAuthorizationInput


class FamilyPortalRecipientSettingsRequest(StrictCamelModel):
    """Só configuração de agenda; não muda autorização nem contatos."""

    expected_version: Annotated[int, Field(ge=1)]
    appointments_enabled: bool


class FamilyPortalWithdrawRequest(StrictCamelModel):
    reason: RecipientWithdrawReason


class FamilyPortalGrantResponse(CamelModel):
    """Metadados do grant — nunca token, hash ou URL recuperável."""

    id: str
    recipient_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    status: FamilyPortalGrantStatus


class FamilyPortalGrantIssueRequest(StrictCamelModel):
    """Emissão/rotação: com link anterior, ``rotateFromGrantId`` é obrigatório."""

    expires_in_days: Annotated[int, Field(ge=1, le=30)] = 30
    expected_recipient_version: Annotated[int, Field(ge=1)]
    rotate_from_grant_id: UUID | None = None


class FamilyPortalGrantIssuedResponse(FamilyPortalGrantResponse):
    url: str


class FamilyPortalRecipientResponse(CamelModel):
    id: str
    caregiver_id: str | None
    recipient_label: str
    active: bool
    appointments_enabled: bool
    version: int
    authorization_version: int
    authorized_at: datetime | None
    recorded_at: datetime | None
    current_grant: FamilyPortalGrantResponse | None


class FamilyPortalEventResponse(CamelModel):
    """Trilha append-only; ``authorization`` só nos eventos de autorização."""

    id: str
    type: str
    occurred_at: datetime
    actor_professional_id: str
    recipient_id: str | None
    item_id: str | None
    grant_id: str | None
    reason: str | None
    authorization: dict | None


# --------------------------------------------------------------------------- #
# Leitura pública (§3.4)
# --------------------------------------------------------------------------- #


class PublicFamilyPortalProfessional(CamelModel):
    name: str
    council: str | None


class PublicFamilyPortalResponse(CamelModel):
    """Projeção mínima da família: sem IDs internos, contatos ou diagnóstico."""

    patient_first_name: str
    professional: PublicFamilyPortalProfessional
    expires_at: datetime
    timezone: str
    appointments_enabled: bool
    sections: list[str]
