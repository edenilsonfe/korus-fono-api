"""F14 onda 2 — DTOs editoriais do portal e projeções públicas de conteúdo.

Payloads novos usam ``extra="forbid"`` e JSON camelCase via ``CamelModel``.
O conteúdo editorial é uma união discriminada por ``kind`` validada pelo
servidor — nunca JSON genérico fornecido pelo cliente. ``material``/``report``
já têm modelo fechado (M2), mas o HTTP da onda 2 aceita apenas
``session_summary``/``goal``/``notice``; os demais falham explicitamente.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import CamelModel
from app.schemas.family_portal import StrictCamelModel, _strip_required

FamilyPortalItemKind = Literal[
    "session_summary", "goal", "notice", "material", "report"
]
FamilyPortalItemStatus = Literal["draft", "published", "withdrawn"]
FamilyPortalFamilyStatus = Literal["practicing", "achieved", "paused"]
FamilyPortalSourceKind = Literal["session", "goal", "resource", "reportDelivery"]

# Tipos implementados no HTTP do editor (onda 3 habilita material/report).
AVAILABLE_ITEM_KINDS = ("session_summary", "goal", "notice", "material", "report")
# Tipos públicos aceitos nas listagens/leituras do portal da família.
PUBLIC_ITEM_KINDS = ("session_summary", "goal", "material", "report", "notice")

PUBLIC_APPOINTMENT_LABEL = "Sessão de fonoaudiologia"


# --------------------------------------------------------------------------- #
# Conteúdo editorial por kind (validado pelo servidor)
# --------------------------------------------------------------------------- #


class SessionSummaryContent(StrictCamelModel):
    """Resumo manual de sessão: texto próprio, nunca cópia de notes/objetivos."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: Annotated[str, Field(min_length=1, max_length=3000)]

    @field_validator("title", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return _strip_required(value)


class GoalContent(StrictCamelModel):
    """Meta simples em linguagem familiar; estado é declaração revisada."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: Annotated[str, Field(min_length=1, max_length=2000)]
    family_status: FamilyPortalFamilyStatus

    @field_validator("title", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return _strip_required(value)


class NoticeContent(StrictCamelModel):
    """Aviso direcionado com expiração obrigatória de 1 a 30 dias."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: Annotated[str, Field(min_length=1, max_length=2000)]
    expires_in_days: Annotated[int, Field(ge=1, le=30)]

    @field_validator("title", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return _strip_required(value)


class MaterialContent(StrictCamelModel):
    """Material selecionado (onda 3): título + instrução familiar."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    instructions: Annotated[str, Field(max_length=2000)] = ""


class ReportContent(StrictCamelModel):
    """Relatório pais (onda 3): o texto vem do snapshot F1, não daqui."""

    title: Annotated[str, Field(min_length=1, max_length=160)]


CONTENT_MODELS: dict[str, type[StrictCamelModel]] = {
    "session_summary": SessionSummaryContent,
    "goal": GoalContent,
    "notice": NoticeContent,
    "material": MaterialContent,
    "report": ReportContent,
}


# --------------------------------------------------------------------------- #
# Fontes por kind
# --------------------------------------------------------------------------- #


class SessionSummarySource(StrictCamelModel):
    session_id: UUID
    evolution_id: UUID | None = None


class GoalSource(StrictCamelModel):
    goal_id: UUID


class MaterialSource(StrictCamelModel):
    resource_id: UUID


class ReportSource(StrictCamelModel):
    delivery_id: UUID


# --------------------------------------------------------------------------- #
# Administração editorial privada (§3.3)
# --------------------------------------------------------------------------- #


class FamilyPortalItemCreateRequest(StrictCamelModel):
    """POST P/items: rascunho com destinatários opcionais (lista vazia ok)."""

    kind: FamilyPortalItemKind
    source: dict[str, Any] | None = None
    content: dict[str, Any]
    recipient_ids: Annotated[list[UUID], Field(max_length=10)] = []


class FamilyPortalItemUpdateRequest(StrictCamelModel):
    """PATCH P/items: kind/source são imutáveis (criar outro item)."""

    expected_version: Annotated[int, Field(ge=1)]
    content: dict[str, Any] | None = None
    recipient_ids: Annotated[list[UUID], Field(max_length=10)] | None = None


class FamilyPortalItemPublishRequest(StrictCamelModel):
    """Publicação revisada; fingerprint da fonte exigido quando há fonte."""

    expected_version: Annotated[int, Field(ge=1)]
    expected_source_fingerprint: Annotated[
        str | None, Field(min_length=1, max_length=64)
    ] = None
    reviewed: Literal[True]


class FamilyPortalItemWithdrawRequest(StrictCamelModel):
    """Corpo vazio; existe para rejeitar campos desconhecidos."""


class FamilyPortalItemResponse(CamelModel):
    """Item privado completo; fingerprint/hash nunca vão para a família."""

    id: str
    kind: str
    source: dict | None
    draft_content: dict
    draft_recipient_ids: list[str]
    status: str
    version: int
    published_version: int | None
    has_unpublished_changes: bool
    source_fingerprint: str | None
    source_changed: bool
    published_at: datetime | None
    updated_at: datetime


class FamilyPortalItemSummary(CamelModel):
    """Resumo de listagem privada; título vem do rascunho vigente."""

    id: str
    kind: str
    title: str
    status: str
    version: int
    published_version: int | None
    has_unpublished_changes: bool
    source_changed: bool
    recipient_ids: list[str]
    published_at: datetime | None


class FamilyPortalItemRevisionResponse(CamelModel):
    """Histórico privado: texto aprovado e metadados mínimos do servidor."""

    version: int
    published_at: datetime
    published_by_professional_id: str
    content: dict
    recipient_ids: list[str]
    source_fingerprint: str | None
    source_metadata: dict


class FamilyPortalSourceCandidate(CamelModel):
    """Candidato do picker privado; nunca devolve notas/answers/contatos."""

    id: str
    kind: str
    label: str
    date: date | None
    source_fingerprint: str
    eligible: bool
    unavailable_reason: str | None


# --------------------------------------------------------------------------- #
# Projeções públicas (§3.4)
# --------------------------------------------------------------------------- #


class PublicItemSummary(CamelModel):
    """Listagem pública: sem IDs de fonte, contatos ou revisão inteira."""

    id: str
    kind: str
    title: str
    published_at: datetime
    session_on: date | None
    family_status: str | None
    available: bool


class PublicItemDetail(CamelModel):
    """Detalhe público; campos específicos por kind, comuns sempre presentes.

    ``material`` acrescenta instrução/atribuição/tipo/tamanho do arquivo;
    ``report`` carrega o snapshot F1 congelado (texto + identidade textual).
    Disponibilidade mudou -> só campos comuns + ``unavailableReason`` genérico.
    """

    id: str
    kind: str
    title: str
    published_at: datetime
    available: bool
    body: str | None = None
    session_on: date | None = None
    family_status: str | None = None
    expires_at: datetime | None = None
    unavailable_reason: str | None = None
    # material
    instructions: str | None = None
    attribution: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    # report (snapshot F1; identidade textual originalmente revisada)
    content: str | None = None
    report_date: date | None = None
    report_version: int | None = None
    content_hash: str | None = None
    patient_name: str | None = None
    professional_name: str | None = None
    professional_council: str | None = None


class PublicFamilyPortalAppointment(CamelModel):
    """Projeção mínima da agenda: sem serviço, preço, tipo livre ou série."""

    id: str
    label: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    status: str
