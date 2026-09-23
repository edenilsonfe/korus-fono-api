from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.schemas.common import CamelModel, PaginatedResponse

ReviewKind = Literal["periodic", "discharge"]
ReviewStatus = Literal["draft", "completed", "cancelled"]
ReviewSourceKind = Literal[
    "assessment", "comparison", "evolution", "session", "goal", "program_measurement", "family_checkin"
]


class StrictCamelModel(CamelModel):
    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")


class ReviewSourceInput(StrictCamelModel):
    kind: ReviewSourceKind
    source_id: UUID = Field(alias="sourceId")
    comparison_target_id: UUID | None = Field(default=None, alias="comparisonTargetId")
    version: int | None = Field(default=None, ge=1)
    fingerprint: str | None = Field(default=None, min_length=1, max_length=64)


class ReviewSourceSnapshot(StrictCamelModel):
    kind: ReviewSourceKind
    source_id: UUID = Field(alias="sourceId")
    comparison_target_id: UUID | None = Field(default=None, alias="comparisonTargetId")
    version: int = Field(ge=1)
    fingerprint: str = Field(min_length=1, max_length=64)
    excerpt: str | None = Field(default=None, max_length=1000)
    source_date: date | None = None
    author_id: UUID | None = None
    author_name: str | None = None
    available: bool = True


class GoalDecision(StrictCamelModel):
    goal_id: UUID
    decision: Literal["maintain", "adjust", "close"]
    note: str = Field(default="", max_length=2000)


class GoalChange(StrictCamelModel):
    goal_id: UUID
    title: str | None = Field(default=None, max_length=255)
    area: str | None = Field(default=None, max_length=100)
    progress: int | None = Field(default=None, ge=0, le=100)
    status: str | None = Field(default=None, max_length=64)


class AppointmentDecision(StrictCamelModel):
    appointment_id: UUID
    action: Literal["keep", "cancel"]


class ClinicalReviewCreate(CamelModel):
    kind: ReviewKind = "periodic"
    period_start: date | None = None
    period_end: date | None = None
    summary: str = Field(default="", max_length=20000)
    goal_decisions: list[GoalDecision] = Field(default_factory=list, max_length=100)
    sources: list[ReviewSourceInput] = Field(default_factory=list, max_length=100)
    next_review_on: date | None = None
    supersedes_review_id: UUID | None = None
    discharge_on: date | None = None
    discharge_reason: str | None = Field(default=None, max_length=2000)
    final_summary: str | None = Field(default=None, max_length=20000)
    family_guidance: str | None = Field(default=None, max_length=20000)
    return_recommended: bool | None = None
    return_on: date | None = None
    home_program_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_period(self):
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("O fim do período não pode ser anterior ao início")
        if self.kind == "discharge" and self.discharge_on and self.discharge_on > date.today():
            raise ValueError("A data efetiva da alta não pode ser futura")
        return self


class ClinicalReviewUpdate(CamelModel):
    expected_version: int = Field(ge=1)
    period_start: date | None = None
    period_end: date | None = None
    summary: str | None = Field(default=None, max_length=20000)
    goal_decisions: list[GoalDecision] | None = Field(default=None, max_length=100)
    sources: list[ReviewSourceInput] | None = Field(default=None, max_length=100)
    next_review_on: date | None = None
    discharge_on: date | None = None
    discharge_reason: str | None = Field(default=None, max_length=2000)
    final_summary: str | None = Field(default=None, max_length=20000)
    family_guidance: str | None = Field(default=None, max_length=20000)
    return_recommended: bool | None = None
    return_on: date | None = None
    home_program_ids: list[UUID] | None = Field(default=None, max_length=50)


class ClinicalReviewComplete(CamelModel):
    expected_version: int = Field(ge=1)
    source_fingerprint: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reviewed: Literal[True]
    confirm_goal_changes: bool = False
    goal_changes: list[GoalChange] = Field(default_factory=list, max_length=100)
    expected_agenda_fingerprint: str | None = Field(default=None, max_length=64)
    appointment_decisions: list[AppointmentDecision] = Field(default_factory=list, max_length=200)


class ClinicalReviewCancel(CamelModel):
    expected_version: int = Field(ge=1)


class FamilyReportCreate(CamelModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


class DischargeAppointmentPreview(CamelModel):
    id: UUID
    date: date
    time: str
    duration: int
    type: str
    status: str
    action_required: bool = True
    can_cancel: bool = True
    blocking_reason: str | None = None
    has_session: bool = False
    has_financial_link: bool = False


class DischargeProgramPreview(CamelModel):
    id: UUID
    title: str
    status: str
    starts_on: date
    ends_on: date
    active_grant: bool = False


class DischargeFamilyGrantPreview(CamelModel):
    id: UUID
    program_id: UUID
    caregiver_name: str
    expires_at: datetime


class FamilyPortalGrantPreview(CamelModel):
    id: UUID
    recipient_id: UUID
    expires_at: datetime


class DischargePreview(CamelModel):
    review_id: UUID
    agenda_fingerprint: str
    appointments: list[DischargeAppointmentPreview]
    programs: list[DischargeProgramPreview] = Field(default_factory=list)
    family_grants: list[DischargeFamilyGrantPreview] = Field(default_factory=list)
    family_portal_grants: list[FamilyPortalGrantPreview] = Field(default_factory=list)


class ClinicalReviewResponse(CamelModel):
    id: UUID
    patient_id: UUID
    author_professional_id: UUID
    kind: ReviewKind
    status: ReviewStatus
    version: int
    period_start: date | None
    period_end: date | None
    summary: str
    goal_decisions: list[GoalDecision]
    sources: list[ReviewSourceSnapshot]
    source_fingerprint: str | None
    next_review_on: date | None
    completed_at: datetime | None
    completed_by_professional_id: UUID | None
    supersedes_review_id: UUID | None
    discharge_on: date | None
    discharge_reason: str | None
    final_summary: str | None
    family_guidance: str | None
    return_recommended: bool | None
    return_on: date | None
    home_program_ids: list[UUID]
    agenda_fingerprint: str | None
    appointment_decisions: list[AppointmentDecision]
    family_report_id: UUID | None


class ClinicalReviewSourceResponse(CamelModel):
    kind: ReviewSourceKind
    source_id: UUID = Field(alias="sourceId")
    comparison_target_id: UUID | None = Field(default=None, alias="comparisonTargetId")
    version: int
    fingerprint: str
    source_date: date | None
    label: str
    excerpt: str | None = None
    author_id: UUID | None = None
    author_name: str | None = None


class ClinicalReviewSourcePage(PaginatedResponse[ClinicalReviewSourceResponse]):
    pass


class DueClinicalReviewResponse(CamelModel):
    id: UUID
    patient_id: UUID
    patient_name: str
    next_review_on: date
    kind: ReviewKind
