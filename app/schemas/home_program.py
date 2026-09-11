"""F16 — DTOs do programa de casa (prescrição, publicação e grants).

Payloads novos usam ``extra="forbid"``; JSON camelCase via ``CamelModel``.
Os DTOs públicos da resposta familiar pertencem à Tarefa 5.2.
"""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.common import CamelModel

HomeProgramStatus = Literal["draft", "active", "archived"]

MAX_TASKS = 50
MAX_PERIOD_DAYS = 30
MAX_RESOURCES_PER_TASK = 5
MAX_CHECK_IN_COMMENT = 2000


class StrictCamelModel(CamelModel):
    """CamelCase com rejeição de campos desconhecidos (422)."""

    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")


def _strip_required(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("O texto não pode ficar em branco")
    return value


def _normalize_comment(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_CHECK_IN_COMMENT:
        raise ValueError(
            f"O comentário deve ter no máximo {MAX_CHECK_IN_COMMENT} caracteres"
        )
    return value


class HomeProgramTaskInput(StrictCamelModel):
    """Tarefa da prescrição; ``id`` é o client id (exclusivo no programa)."""

    id: UUID | None = None
    title: Annotated[str, Field(min_length=1, max_length=160)]
    instructions: Annotated[str, Field(min_length=1, max_length=4000)]
    due_on: date
    goal_id: UUID | None = None
    intervention_program_id: UUID | None = None
    resource_ids: Annotated[
        list[UUID], Field(max_length=MAX_RESOURCES_PER_TASK)
    ] = Field(default_factory=list)

    @field_validator("title", "instructions")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return _strip_required(value)

    @model_validator(mode="after")
    def validate_target_and_resources(self):
        has_goal = self.goal_id is not None
        has_program = self.intervention_program_id is not None
        if has_goal == has_program:
            raise ValueError(
                "Informe exatamente uma meta ou um programa ABA por tarefa"
            )
        if len(set(self.resource_ids)) != len(self.resource_ids):
            raise ValueError("Recursos repetidos na mesma tarefa")
        return self


class HomeProgramCreate(StrictCamelModel):
    title: Annotated[str, Field(min_length=1, max_length=160)]
    starts_on: date
    ends_on: date
    tasks: Annotated[
        list[HomeProgramTaskInput], Field(min_length=1, max_length=MAX_TASKS)
    ]

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return _strip_required(value)

    @model_validator(mode="after")
    def validate_client_task_ids(self):
        client_ids = [task.id for task in self.tasks if task.id is not None]
        if len(set(client_ids)) != len(client_ids):
            raise ValueError("Os identificadores de tarefa devem ser exclusivos")
        return self


class HomeProgramUpdate(StrictCamelModel):
    expected_version: Annotated[int, Field(ge=1)]
    title: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    starts_on: date | None = None
    ends_on: date | None = None
    tasks: Annotated[
        list[HomeProgramTaskInput] | None,
        Field(min_length=1, max_length=MAX_TASKS),
    ] = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        return _strip_required(value) if value is not None else None

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set - {"expected_version"}:
            raise ValueError("Informe ao menos uma alteração")
        if self.tasks is not None:
            client_ids = [task.id for task in self.tasks if task.id is not None]
            if len(set(client_ids)) != len(client_ids):
                raise ValueError(
                    "Os identificadores de tarefa devem ser exclusivos"
                )
        return self


class HomeProgramTaskResourceResponse(CamelModel):
    resource_id: str
    title: str
    available: bool
    reason: str | None


class HomeProgramTaskResponse(CamelModel):
    id: str
    client_task_id: str
    title: str
    instructions: str
    due_on: date
    goal_id: str | None
    intervention_program_id: str | None
    resources: list[HomeProgramTaskResourceResponse]


class HomeProgramResponse(CamelModel):
    id: str
    patient_id: str
    title: str
    status: HomeProgramStatus
    version: int
    starts_on: date
    ends_on: date
    timezone: str
    tasks: list[HomeProgramTaskResponse]
    created_at: datetime
    updated_at: datetime


class HomeProgramPublishRequest(StrictCamelModel):
    expected_version: Annotated[int, Field(ge=1)]


class HomeProgramArchiveRequest(StrictCamelModel):
    """Corpo vazio; existe para rejeitar campos desconhecidos."""


class HomeProgramFamilyAuthorization(StrictCamelModel):
    """Declaração específica da família para este programa de casa.

    Não substitui o consentimento da equipe assistencial nem autoriza outros
    usos; fica restrita ao grant emitido.
    """

    authorized_at: datetime
    reference: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    reviewed: Literal[True]

    @field_validator("reference")
    @classmethod
    def strip_reference(cls, value: str | None) -> str | None:
        return _strip_required(value) if value is not None else None


class HomeProgramGrantCreate(StrictCamelModel):
    caregiver_id: UUID
    expires_in_days: Annotated[int, Field(ge=1, le=30)] = 14
    family_authorization: HomeProgramFamilyAuthorization


class HomeProgramGrantResponse(CamelModel):
    id: str
    caregiver_id: str | None
    recipient_label: str
    expires_at: datetime
    revoked_at: datetime | None
    url: str | None = None


class HomeProgramCheckInCreate(StrictCamelModel):
    """Comando público de criação (idempotente por ``clientRecordId``)."""

    client_record_id: UUID
    done: bool
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        return _normalize_comment(value)


class HomeProgramCheckInUpdate(StrictCamelModel):
    """Comando público de edição com controle de versão otimista."""

    client_record_id: UUID
    expected_version: Annotated[int, Field(ge=1)]
    done: bool
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        return _normalize_comment(value)


class HomeProgramCheckInResponse(CamelModel):
    """Resposta familiar registrada (timestamps e autoria são do servidor)."""

    id: str
    task_id: str
    done: bool
    comment: str | None
    responded_at: datetime
    version: int
    has_photo: bool


class HomeProgramPhotoResponse(CamelModel):
    """Foto vigente da resposta (Tarefa 5.3).

    ``version`` é a versão do CHECK-IN usada como controle otimista do comando —
    a foto é independente de marcar feito e não altera o texto da resposta.
    """

    id: str | None = None
    has_photo: bool
    version: int


class HomeProgramPhotoDeleteRequest(StrictCamelModel):
    """Comando público de remoção da foto (idempotente por ``clientRecordId``)."""

    client_record_id: UUID
    expected_version: Annotated[int, Field(ge=1)]


class HomeProgramCheckInSummaryResponse(CamelModel):
    """Acompanhamento profissional da resposta familiar (autodeclarada).

    Contato e nome completo do responsável só aparecem para o dono; os demais
    leitores clínicos veem o rótulo neutro "Responsável".
    """

    id: str
    task_id: str
    task_title: str
    done: bool
    comment: str | None
    responded_at: datetime
    version: int
    has_photo: bool
    actor_label: str


class PublicHomeProgramMaterialResponse(CamelModel):
    id: str
    title: str
    attribution: str | None
    available: bool


class PublicHomeProgramCheckInResponse(CamelModel):
    id: str
    done: bool
    comment: str | None
    responded_at: datetime
    version: int
    has_photo: bool


class PublicHomeProgramTaskResponse(CamelModel):
    id: str
    title: str
    instructions: str
    due_on: date
    materials: list[PublicHomeProgramMaterialResponse]
    check_in: PublicHomeProgramCheckInResponse | None


class PublicHomeProgramResponse(CamelModel):
    """Projeção mínima da família (sem metas, critérios, contatos ou IDs alheios)."""

    id: str
    title: str
    patient_first_name: str
    professional_name: str
    starts_on: date
    ends_on: date
    expires_at: datetime
    can_respond: bool
    tasks: list[PublicHomeProgramTaskResponse]
