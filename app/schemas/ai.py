from pydantic import Field

from app.schemas.common import CamelModel


class AIReportCreate(CamelModel):
    patient_id: str
    type: str  # clinico | escolar | pais | evolutivo
    prompt: str | None = None


class AIReportResponse(CamelModel):
    id: str
    type: str
    patient_id: str
    patient: str
    date: str
    preview: str
    content: str
    status: str


class AIReportUpdate(CamelModel):
    content: str
    status: str | None = None


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
