from typing import Any, Optional

from pydantic import Field

from app.schemas.common import CamelModel


class NormsStatusDto(CamelModel):
    level: str
    label: str
    detail: str
    show_standard_scores: bool = False
    source: Optional[str] = None


class InstrumentManifestResponse(CamelModel):
    package_id: str
    instrument_slug: str
    instrument_title: str
    version: int
    archetype: str
    scale: list[dict[str, Any]]
    domains: list[dict[str, str]]
    item_count: int
    scoring_engine: Optional[str] = None
    license_ref: Optional[str] = None
    content_status: Optional[str] = None
    administration_notes: Optional[str] = None
    report: dict[str, Any] = Field(default_factory=dict)
    subtests: list[dict[str, Any]] = Field(default_factory=list)
    modules: list[dict[str, Any]] = Field(default_factory=list)
    informant_forms: list[dict[str, Any]] = Field(default_factory=list)
    requires_competency_ack: bool = False
    supports_multi_session: bool = False
    norms_region: Optional[str] = None
    has_norms: bool = False
    norms_status: Optional[NormsStatusDto] = None


class InstrumentContentItem(CamelModel):
    id: str
    domain: Optional[str] = None
    text: str
    section: Optional[str] = None
    module: Optional[str] = None


class InstrumentContentResponse(CamelModel):
    instrument_slug: str
    scale: list[dict[str, Any]]
    domains: list[dict[str, str]]
    items: list[InstrumentContentItem]
    page: int
    page_size: int
    total_items: int
    total_pages: int


class InstrumentScoreRequest(CamelModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class InstrumentScoreResponse(CamelModel):
    engine: str
    domains: dict[str, Any]
    total: Optional[int | float] = None
    interpretation: Optional[str] = None
    level_label: Optional[str] = None
    summary: str
    detail: Optional[str] = None
    suggested_goals: list[dict[str, Any]] = Field(default_factory=list)
    cutoffs: list[dict[str, Any]] = Field(default_factory=list)
    subtests: dict[str, Any] = Field(default_factory=dict)
    pending_modules: list[str] = Field(default_factory=list)
    norms_status: Optional[NormsStatusDto] = None


class ProtocolCapabilitiesResponse(CamelModel):
    protocol_id: str
    scoring_mode: str  # manifest | client | battery | manual
    instrument_slug: Optional[str] = None
    has_items: bool = False
