from pydantic import Field

from app.schemas.common import CamelModel
from app.schemas.patient import AssessmentResponse


class ProtocolFieldTemplate(CamelModel):
    key: str
    label: str


class ProtocolResponse(CamelModel):
    id: str
    name: str
    full_name: str
    description: str
    age_range: str
    fields: list[ProtocolFieldTemplate]
    applications: int = 0
    avg_result: float = 0
    last_applied: str | None = None
    scoring_mode: str = "manual"


class AssessmentFieldInput(CamelModel):
    label: str
    value: str


class AssessmentCreate(CamelModel):
    protocol_id: str
    date: str | None = None
    result: str = ""
    percentage: int = 0
    interpretation: str = ""
    fields: list[AssessmentFieldInput] = Field(default_factory=list)
    answers: dict = Field(default_factory=dict)
    scores: dict | None = None
    status: str = "completed"
    informant: str | None = None
    metadata: dict | None = None


class AssessmentDraftUpsert(CamelModel):
    answers: dict = Field(default_factory=dict)
    informant: str | None = None
    metadata: dict | None = None


class AssessmentFinalize(CamelModel):
    date: str | None = None
    result: str = ""
    percentage: int = 0
    interpretation: str = ""
    fields: list[AssessmentFieldInput] = Field(default_factory=list)
    answers: dict = Field(default_factory=dict)
    scores: dict | None = None
    informant: str | None = None
    metadata: dict | None = None


class AssessmentStatusCounts(CamelModel):
    all: int = 0
    draft: int = 0
    awaiting_informant: int = 0
    completed: int = 0
    cancelled: int = 0


class AssessmentsPage(CamelModel):
    items: list[AssessmentResponse]
    total: int
    page: int
    limit: int
    status_counts: AssessmentStatusCounts


class AssessmentCancelResponse(CamelModel):
    id: str
    status: str


class GoalCreate(CamelModel):
    title: str
    area: str
    progress: int = 0
    start_date: str | None = None
    status: str | None = None


class GoalUpdate(CamelModel):
    title: str | None = None
    area: str | None = None
    progress: int | None = None
    status: str | None = None
