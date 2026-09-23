"""Closed, versioned DTOs for the pediatric pre-attendance form."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from app.schemas.common import CamelModel
from app.schemas.prontuario import AnamneseEntryResponse

INTAKE_FORM_VERSION = "pediatric-v1"
INTAKE_FORM_KEYS = (
    "reasonForReferral", "developmentHistory", "schooling", "currentCare",
    "routine", "expectations", "additionalNotes",
)


class IntakeModel(CamelModel):
    model_config = ConfigDict(**CamelModel.model_config, extra="forbid")


class IntakeAnswer(IntakeModel):
    value: Annotated[str | None, Field(max_length=5000)] = None
    not_known: bool = False

    @field_validator("value")
    @classmethod
    def trim_value(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class IntakeCreate(IntakeModel):
    caregiver_id: UUID
    form_version: Literal["pediatric-v1"] = INTAKE_FORM_VERSION


class IntakeDraftPatch(IntakeModel):
    expected_version: Annotated[int, Field(ge=1)]
    responses: dict[str, IntakeAnswer]


class IntakeSubmit(IntakeModel):
    expected_version: Annotated[int, Field(ge=1)]
    command_key: Annotated[str, Field(min_length=1, max_length=128)]
    responses: dict[str, IntakeAnswer] | None = None


class IntakeGrantIssue(IntakeModel):
    expires_in_days: Annotated[int, Field(ge=1, le=14)] = 14
    rotate_from_grant_id: UUID | None = None
    authorized_at: datetime
    reference: Annotated[str | None, Field(max_length=500)] = None
    reviewed: Literal[True]


class IntakeReview(IntakeModel):
    expected_version: Annotated[int, Field(ge=1)]
    anamnese_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    selected_fields: list[str] = Field(default_factory=list, max_length=len(INTAKE_FORM_KEYS))
    selected_file_ids: list[UUID] = Field(default_factory=list, max_length=5)
    command_key: Annotated[str, Field(min_length=1, max_length=128)]


class IntakeFileResponse(IntakeModel):
    id: str
    name: str
    content_type: str
    size_bytes: int
    created_at: datetime
    incorporated: bool = False


class IntakeGrantResponse(IntakeModel):
    id: str
    expires_at: datetime
    revoked_at: datetime | None = None
    active: bool


class IntakeGrantIssuedResponse(IntakeGrantResponse):
    url: str


class IntakeRequestResponse(IntakeModel):
    id: str
    patient_id: str
    caregiver_id: str | None
    caregiver_name: str
    form_version: str
    status: Literal["draft", "submitted", "reviewed", "cancelled"]
    responses: dict[str, IntakeAnswer]
    version: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    anamnese_fingerprint: str | None = None
    anamnese_status: str
    current_anamnese: list[AnamneseEntryResponse] = Field(default_factory=list)
    files: list[IntakeFileResponse] = Field(default_factory=list)
    current_grant: IntakeGrantResponse | None = None
    selected_fields: list[str] = Field(default_factory=list)
    selected_file_ids: list[str] = Field(default_factory=list)


class IntakePublicResponse(IntakeModel):
    patient_first_name: str
    professional_name: str
    can_respond: bool
    form_version: str
    status: Literal["draft", "submitted", "reviewed"]
    responses: dict[str, IntakeAnswer]
    version: int
    expires_at: datetime
    files: list[IntakeFileResponse] = Field(default_factory=list)


class IntakeReviewResponse(IntakeRequestResponse):
    selected_fields: list[str] = Field(default_factory=list)
    selected_file_ids: list[str] = Field(default_factory=list)
