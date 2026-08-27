from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import CamelModel, PaginatedResponse

AdminRole = Literal["support", "billing", "product", "superadmin"]


class SetAdminRoleBody(CamelModel):
    admin_role: AdminRole | None = None
    reason: str = Field(min_length=5, max_length=500)


class AdminAuditEventItem(CamelModel):
    id: str
    actor_id: str | None = None
    actor_name: str | None = None
    actor_email: str | None = None
    target_professional_id: str | None = None
    action: str
    reason: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class AdminAuditEventsPage(PaginatedResponse[AdminAuditEventItem]):
    pass


AttentionKind = Literal[
    "billing_divergence",
    "trial_email_failed",
    "trial_email_stuck",
    "ai_job_stuck",
    "whatsapp_disconnected",
]


class AdminAttentionItem(CamelModel):
    id: str
    kind: AttentionKind
    severity: Literal["warning", "critical"]
    title: str
    description: str
    target_url: str
    professional_id: str | None = None
    created_at: datetime | None = None


class AdminAttentionResponse(CamelModel):
    items: list[AdminAttentionItem]
    total: int
    generated_at: datetime
