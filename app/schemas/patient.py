from datetime import date as DateType
from typing import Literal, TypeAlias

from pydantic import EmailStr, Field, TypeAdapter, field_validator

from app.schemas.common import CamelModel

_email_adapter = TypeAdapter(EmailStr)

PatientStatus: TypeAlias = Literal["ativo", "avaliacao", "pausado", "alta", "inativo"]


def _normalize_optional_email(value: str | None) -> str:
    """Allow blank caregiver email; otherwise require EmailStr."""
    if value is None:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise ValueError("E-mail contém caracteres inválidos")
    return str(_email_adapter.validate_python(normalized))


class CaregiverCreate(CamelModel):
    name: str
    relation: str = ""
    phone: str = ""
    email: str = ""
    notes: str = ""
    contact: str | None = None  # alias from frontend AddPatientDialog
    is_primary: bool = False
    whatsapp_opt_in: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str:
        return _normalize_optional_email(value)


class CaregiverUpdate(CamelModel):
    name: str | None = None
    relation: str | None = None
    phone: str | None = None
    email: str | None = None
    notes: str | None = None
    is_primary: bool | None = None
    whatsapp_opt_in: bool | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_optional_email(value)


class CaregiverResponse(CamelModel):
    id: str
    name: str
    relation: str
    phone: str
    email: str
    notes: str = ""
    is_primary: bool = False
    whatsapp_opt_in: bool = False


class PatientCreate(CamelModel):
    name: str
    birth_date: DateType
    diagnosis_keys: list[str] = Field(min_length=1)
    status: PatientStatus = "avaliacao"
    guardians: list[CaregiverCreate] = Field(default_factory=list)


class PatientUpdate(CamelModel):
    name: str | None = None
    birth_date: DateType | None = None
    diagnosis_keys: list[str] | None = None
    status: PatientStatus | None = None

    @field_validator("diagnosis_keys")
    @classmethod
    def validate_diagnosis_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) < 1:
            raise ValueError("Pelo menos um diagnóstico é obrigatório")
        return value


class TherapyPlanUpdate(CamelModel):
    content: str


class GoalResponse(CamelModel):
    id: str
    title: str
    progress: int
    area: str
    professional: str
    start_date: str
    status: str


class ClinicalDomainResponse(CamelModel):
    key: str
    label: str
    score: int
    delta: int
    history: list[int]


class ClinicalDomainHistoryPoint(CamelModel):
    date: str
    score: int


class DevelopmentAnalyticsAreaResponse(CamelModel):
    """Shape for GET /analytics/development — history includes dates."""

    key: str
    label: str
    score: int
    delta: int
    history: list[ClinicalDomainHistoryPoint]


class AssessmentField(CamelModel):
    label: str
    value: str


class AssessmentResponse(CamelModel):
    id: str
    protocol: str
    protocol_id: str
    date: str
    professional: str
    result: str
    percentage: int
    interpretation: str
    fields: list[AssessmentField] = Field(default_factory=list)
    patient_id: str | None = None
    patient_name: str | None = None
    avatar_color: str | None = None
    answers: dict = Field(default_factory=dict)
    scores: dict | None = None
    status: str = "completed"
    informant: str | None = None
    metadata: dict | None = None


class SessionResponse(CamelModel):
    id: str
    date: str
    duration: int
    therapist: str
    objectives: list[str]
    notes: str
    type: str


class TimelineEventResponse(CamelModel):
    id: str
    type: str
    title: str
    description: str
    date: str
    patient_id: str | None = None
    patient_name: str | None = None
    source_id: str | None = None


class AttachmentResponse(CamelModel):
    id: str
    name: str
    category: str
    date: str
    size_bytes: int
    size: str | None = None  # formatted for UI compat


class PatientSummary(CamelModel):
    id: str
    name: str
    age: int
    birth_date: str
    guardian: str
    guardian_label: str
    diagnoses: list[str]
    diagnosis_keys: list[str]
    therapist: str
    status: str
    start_date: str
    last_session: str | None = None
    sessions_count: int
    protocols_done: int
    goals_achieved: int
    total_goals: int
    avatar_color: str
    is_demo: bool = False
    therapy_plan_content: str | None = None
    therapy_plan_updated_at: str | None = None


class PatientDetail(PatientSummary):
    caregivers: list[CaregiverResponse] = Field(default_factory=list)
    goals: list[GoalResponse] = Field(default_factory=list)
    clinical_domains: list[ClinicalDomainResponse] = Field(default_factory=list)
    assessments: list[AssessmentResponse] = Field(default_factory=list)
    sessions: list[SessionResponse] = Field(default_factory=list)
    timeline: list[TimelineEventResponse] = Field(default_factory=list)
    files: list[AttachmentResponse] = Field(default_factory=list)


class BulkAppointmentCancellationResponse(CamelModel):
    cancelled_count: int
