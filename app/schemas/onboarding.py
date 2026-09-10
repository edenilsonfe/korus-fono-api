from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from app.schemas.common import CamelModel


OnboardingAction = Literal[
    "viewed_demo_patient",
    "viewed_demo_result",
    "reviewed_demo_report",
    "skip",
    "postpone",
    "resume",
]

OnboardingNextStep = Literal[
    "view_demo_patient",
    "complete_demo_assessment",
    "view_demo_result",
    "create_demo_report",
    "review_demo_report",
    "configure_service",
    "create_real_patient",
    "completed",
]


class OnboardingSteps(CamelModel):
    viewed_demo_patient: bool
    completed_demo_assessment: bool
    viewed_demo_result: bool
    created_demo_report: bool
    reviewed_demo_report: bool
    configured_service: bool
    created_real_patient: bool


class OnboardingResponse(CamelModel):
    version: int
    demo_patient_id: str | None
    demo_report_id: str | None
    demo_started_at: datetime | None
    demo_completed_at: datetime | None
    is_demo_complete: bool
    skipped_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    dismissed_until: datetime | None
    is_complete: bool
    next_step: OnboardingNextStep
    steps: OnboardingSteps


class OnboardingUpdate(CamelModel):
    action: OnboardingAction
    report_id: UUID | None = None

    @model_validator(mode="after")
    def require_report_for_review(self):
        if self.action == "reviewed_demo_report" and self.report_id is None:
            raise ValueError("Informe o relatório demonstrativo revisado")
        return self
