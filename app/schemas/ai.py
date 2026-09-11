from typing import Literal
from datetime import datetime

from pydantic import Field, model_validator

from app.schemas.common import CamelModel
from app.schemas.report_composition import ReportCompositionCreate


class AIReportCreate(CamelModel):
    patient_id: str
    type: str  # clinico | escolar | pais | evolutivo | consolidado
    prompt: str | None = Field(default=None, max_length=2000)
    composition: ReportCompositionCreate | None = None

    @model_validator(mode="after")
    def _validate_composition_for_type(self) -> "AIReportCreate":
        if self.type == "consolidado":
            if self.composition is None:
                raise ValueError(
                    "O campo composition é obrigatório para relatórios do tipo consolidado."
                )
        elif self.composition is not None:
            raise ValueError(
                "O campo composition é permitido apenas para relatórios do tipo consolidado."
            )
        return self


class AIReportResponse(CamelModel):
    id: str
    type: str
    patient_id: str
    patient: str
    date: str
    preview: str
    content: str
    status: str
    version: int
    composition_id: str | None = None


class AIReportUpdate(CamelModel):
    content: str
    status: Literal["draft", "finalized"] | None = None
    # Optimistic control (F3): required for consolidated reports, optional for
    # legacy report types so their PATCH contract stays compatible.
    expected_version: int | None = Field(default=None, ge=1)


class AIReportRevisionResponse(CamelModel):
    id: str
    content: str
    status: str
    professional_id: str
    created_at: datetime
    # Null for historical revisions written before numeric versioning (F3).
    version: int | None = None


class AIJobResponse(CamelModel):
    id: str
    job_type: str
    status: str
    result: str | None = None
    error: str | None = None


class ConversationCreate(CamelModel):
    title: str | None = None
    patient_id: str | None = None


class ConversationUpdate(CamelModel):
    title: str


class MessageCreate(CamelModel):
    content: str
    patient_id: str | None = None


class ChatMessageResponse(CamelModel):
    id: str
    role: str
    content: str
    created_at: str


class ConversationResponse(CamelModel):
    id: str
    title: str
    patient_id: str | None = None
    created_at: str
    updated_at: str
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class AIToolRequest(CamelModel):
    patient_id: str | None = None
    text: str | None = None
    prompt: str | None = None
    session_notes: str | None = None


class AICapabilitiesResponse(CamelModel):
    llm_enabled: bool
    audio_transcription_enabled: bool
