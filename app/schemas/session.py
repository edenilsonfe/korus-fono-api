from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import CamelModel


class SessionCreate(CamelModel):
    appointment_id: UUID | None = None
    date: datetime | None = None
    duration: int = 50
    type: str = "Terapia individual"
    objectives: list[str] = Field(default_factory=list)
    notes: str = ""


class SessionUpdate(CamelModel):
    duration: int | None = None
    type: str | None = None
    objectives: list[str] | None = None
    notes: str | None = None


class SessionGlobalResponse(CamelModel):
    id: str
    appointment_id: str | None
    patient_id: str
    patient_name: str
    avatar_color: str
    date: str
    duration: int
    therapist: str
    type: str
    objectives: list[str]
    notes: str
