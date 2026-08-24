from pydantic import Field

from app.schemas.common import CamelModel


class EvolutionCreate(CamelModel):
    title: str | None = None
    content: str
    session_id: str | None = None


class EvolutionResponse(CamelModel):
    id: str
    patient_id: str
    session_id: str | None = None
    date: str
    title: str | None = None
    content: str
    professional: str


class AnamneseCreate(CamelModel):
    section: str
    value: str


class AnamneseEntryInput(CamelModel):
    section: str
    value: str


class AnamneseBulkUpsert(CamelModel):
    entries: list[AnamneseEntryInput] = Field(default_factory=list)


class AnamneseComplete(CamelModel):
    entries: list[AnamneseEntryInput] | None = None


class AnamneseEntryResponse(CamelModel):
    id: str
    patient_id: str
    section: str
    value: str


# Compat alias — same shape as entry.
AnamneseResponse = AnamneseEntryResponse


class AnamneseDocumentResponse(CamelModel):
    status: str
    completed_at: str | None = None
    entries: list[AnamneseEntryResponse]
