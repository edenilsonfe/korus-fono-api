from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.common import CamelModel

ProgramStatus = Literal["draft", "active", "paused", "mastered", "archived"]
ParticipationStatus = Literal["participated", "declined", "withdrew", "not_observed"]
ReviewStatus = Literal["submitted", "approved", "voided"]
MeasurementCount = Annotated[int, Field(ge=0, le=100_000)]
PROMPT_TYPES = frozenset(
    {"verbal", "gestural", "model", "visual", "partial_physical", "full_physical"}
)


class InterventionProgramCreate(CamelModel):
    goal_id: UUID | None = None
    title: Annotated[str, Field(min_length=1, max_length=255)]
    operational_definition: Annotated[str, Field(min_length=1, max_length=4000)]
    teaching_strategy: Annotated[str, Field(min_length=1, max_length=4000)]
    mastery_percent: Annotated[int, Field(ge=1, le=100)]
    mastery_consecutive_sessions: Annotated[int, Field(ge=1, le=20)]
    generalization_criterion: Annotated[str | None, Field(max_length=2000)] = None
    maintenance_criterion: Annotated[str | None, Field(max_length=2000)] = None
    replaces_program_id: UUID | None = None

    @field_validator("title", "operational_definition", "teaching_strategy")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("O texto não pode ficar em branco")
        return value


class InterventionProgramUpdate(CamelModel):
    goal_id: UUID | None = None
    title: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    operational_definition: Annotated[
        str | None, Field(min_length=1, max_length=4000)
    ] = None
    teaching_strategy: Annotated[str | None, Field(min_length=1, max_length=4000)] = (
        None
    )
    mastery_percent: Annotated[int | None, Field(ge=1, le=100)] = None
    mastery_consecutive_sessions: Annotated[int | None, Field(ge=1, le=20)] = None
    generalization_criterion: Annotated[str | None, Field(max_length=2000)] = None
    maintenance_criterion: Annotated[str | None, Field(max_length=2000)] = None
    status: ProgramStatus | None = None

    @field_validator("title", "operational_definition", "teaching_strategy")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("O texto não pode ficar em branco")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("Informe ao menos uma alteração")
        return self


class InterventionProgramResponse(CamelModel):
    id: str
    patient_id: str
    created_by_professional_id: str
    goal_id: str | None
    approach: Literal["aba"]
    title: str
    operational_definition: str
    teaching_strategy: str
    mastery_percent: int
    mastery_consecutive_sessions: int
    generalization_criterion: str | None
    maintenance_criterion: str | None
    status: ProgramStatus
    activated_at: datetime | None
    closed_at: datetime | None
    closed_by_professional_id: str | None
    replaces_program_id: str | None
    mastery_eligible: bool
    created_at: datetime
    updated_at: datetime


class ProgramMeasurementCreate(CamelModel):
    client_record_id: UUID
    session_id: UUID
    participation_status: ParticipationStatus
    opportunities: MeasurementCount = 0
    independent: MeasurementCount = 0
    prompted: MeasurementCount = 0
    incorrect: MeasurementCount = 0
    no_response: MeasurementCount = 0
    prompt_counts: dict[str, MeasurementCount] = Field(default_factory=dict)
    notes: Annotated[str | None, Field(max_length=2000)] = None
    replaces_measurement_id: UUID | None = None

    @field_validator("prompt_counts")
    @classmethod
    def validate_prompt_types(cls, value: dict[str, int]) -> dict[str, int]:
        invalid = set(value) - PROMPT_TYPES
        if invalid:
            raise ValueError(f"Tipos de dica inválidos: {', '.join(sorted(invalid))}")
        return value

    @model_validator(mode="after")
    def validate_counts(self):
        counts = self.independent + self.prompted + self.incorrect + self.no_response
        if self.participation_status == "participated":
            if self.opportunities <= 0 or counts != self.opportunities:
                raise ValueError("Os resultados devem somar o total de oportunidades")
        elif self.opportunities or counts:
            raise ValueError(
                "Ausência ou recusa deve ser registrada com contagens zeradas"
            )
        if sum(self.prompt_counts.values()) > self.prompted:
            raise ValueError(
                "As dicas detalhadas não podem exceder as respostas com dica"
            )
        return self


class ProgramMeasurementResponse(CamelModel):
    id: str
    client_record_id: str
    program_id: str
    session_id: str
    recorded_by_professional_id: str
    recorded_at: datetime
    participation_status: ParticipationStatus
    opportunities: int
    independent: int
    prompted: int
    incorrect: int
    no_response: int
    prompt_counts: dict[str, int]
    notes: str | None
    review_status: ReviewStatus
    reviewed_by_professional_id: str | None
    reviewed_at: datetime | None
    review_reason: str | None
    replaces_measurement_id: str | None


class ProgramMeasurementReview(CamelModel):
    action: Literal["approve", "void"]
    reason: Annotated[str | None, Field(min_length=3, max_length=1000)] = None

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def require_void_reason(self):
        if self.action == "void" and (self.reason is None or len(self.reason) < 3):
            raise ValueError("Informe o motivo para anular a mensuração")
        return self
