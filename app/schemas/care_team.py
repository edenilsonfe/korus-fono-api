from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import EmailStr, Field

from app.schemas.common import CamelModel

CareTeamRole = Literal["supervisor", "practitioner"]
ConsentDecision = Literal["granted", "withdrawn"]


class SharingConsentCreate(CamelModel):
    decision: ConsentDecision
    policy_version: Annotated[str, Field(min_length=1, max_length=64)]
    caregiver_id: UUID | None = None
    notes: Annotated[str | None, Field(max_length=1000)] = None


class SharingConsentResponse(CamelModel):
    id: str
    patient_id: str
    caregiver_id: str | None
    decision: ConsentDecision
    policy_version: str
    recorded_by_professional_id: str
    recorded_at: datetime
    notes: str | None


class CareTeamInvitationCreate(CamelModel):
    email: EmailStr
    role: CareTeamRole


class CareTeamInvitationToken(CamelModel):
    token: Annotated[str, Field(min_length=16, max_length=512)]


class CareTeamMemberUpdate(CamelModel):
    role: CareTeamRole


class CareTeamMemberResponse(CamelModel):
    id: str | None
    patient_id: str
    professional_id: str
    professional_name: str
    specialty: str
    council: str
    role: Literal["coordinator", "supervisor", "practitioner"]
    status: Literal["invited", "active", "declined", "revoked", "expired"]
    invited_at: datetime | None = None
    accepted_at: datetime | None = None


class PatientAccessEventResponse(CamelModel):
    id: str
    patient_id: str
    actor_professional_id: str
    actor_role: str
    action: str
    resource_type: str
    resource_id: str | None
    occurred_at: datetime
