"""DTOs for the consolidated (multi-instrument) report composition — F3."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.common import CamelModel


class ReportCompositionComparisonPair(CamelModel):
    model_config = ConfigDict(extra="forbid")

    base_id: UUID
    target_id: UUID


class ReportCompositionCreate(CamelModel):
    """Explicit selection of sources for a `consolidado` report."""

    model_config = ConfigDict(extra="forbid")

    assessment_ids: list[UUID] = Field(min_length=2, max_length=10)
    evolution_ids: list[UUID] = Field(default_factory=list, max_length=20)
    session_ids: list[UUID] = Field(default_factory=list, max_length=20)
    goal_ids: list[UUID] = Field(default_factory=list, max_length=20)
    comparisons: list[ReportCompositionComparisonPair] = Field(
        default_factory=list, max_length=5
    )
    supersedes_report_id: UUID | None = None


class ReportSourceResponse(CamelModel):
    """Listing item for GET /patients/{id}/report-sources (summary is trimmed)."""

    id: str
    kind: str
    label: str
    date: str
    author_name: str
    protocol_id: str | None = None
    status: str | None = None
    summary: str


class ReportCompositionSourceItem(CamelModel):
    kind: str
    id: str
    label: str
    date: str
    author_name: str
    protocol_id: str | None = None
    source_hash: str


class ReportCompositionMetric(CamelModel):
    key: str
    label: str
    base: float | None = None
    target: float | None = None
    delta: float | None = None


class ReportCompositionComparisonItem(CamelModel):
    base_id: str
    target_id: str
    percentage_delta: int
    metrics: list[ReportCompositionMetric] = Field(default_factory=list)


class ReportCompositionResponse(CamelModel):
    id: str
    report_id: str
    template_version: str
    captured_at: datetime
    context_hash: str
    supersedes_report_id: str | None = None
    sources: list[ReportCompositionSourceItem] = Field(default_factory=list)
    comparisons: list[ReportCompositionComparisonItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
