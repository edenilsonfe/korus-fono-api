"""F17 — schemas de licença de distribuição de recursos (camelCase)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from app.core.resource_catalog import (
    RESOURCE_LICENSE_DECISION_REASON_MAX,
    RESOURCE_LICENSE_DECISION_REASON_MIN,
)
from app.schemas.common import CamelModel

ResourceLicenseStatus = Literal["declared", "pending", "approved", "rejected", "revoked"]
ResourceLicenseOrigin = Literal["original", "licensed", "public_domain"]
ResourceLicenseDecisionKind = Literal["approved", "rejected", "revoked"]


class ResourceLicenseDeclaration(CamelModel):
    """Declaração de licença — usada pelo dono (PUT) e pela curadoria (POST admin)."""

    model_config = ConfigDict(extra="forbid")

    origin: ResourceLicenseOrigin
    rights_holder: str = Field(min_length=1, max_length=255)
    source_reference: str | None = Field(default=None, max_length=500)
    evidence_reference: str | None = Field(default=None, max_length=500)
    attribution: str = Field(default="", max_length=500)
    valid_until: date | None = None
    allow_professional_distribution: bool = False
    allow_family_delivery: bool = False
    declaration_accepted: Literal[True]
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("rights_holder")
    @classmethod
    def _validate_rights_holder(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Informe o titular dos direitos do material.")
        return stripped

    @field_validator("source_reference", "evidence_reference")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("attribution")
    @classmethod
    def _strip_attribution(cls, value: str) -> str:
        return value.strip()


class ResourceLicenseDecisionCreate(CamelModel):
    """Decisão de curadoria sobre uma licença declarada."""

    model_config = ConfigDict(extra="forbid")

    license_id: UUID
    decision: ResourceLicenseDecisionKind
    reason: str = Field(
        min_length=RESOURCE_LICENSE_DECISION_REASON_MIN,
        max_length=RESOURCE_LICENSE_DECISION_REASON_MAX,
    )

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < RESOURCE_LICENSE_DECISION_REASON_MIN:
            raise ValueError(
                f"O motivo deve ter entre {RESOURCE_LICENSE_DECISION_REASON_MIN} e "
                f"{RESOURCE_LICENSE_DECISION_REASON_MAX} caracteres."
            )
        return stripped


class ResourcePublicationUpdate(CamelModel):
    """Publicação/arquivamento editorial de um recurso (curadoria)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["published", "archived"]
    reason: str = Field(
        min_length=RESOURCE_LICENSE_DECISION_REASON_MIN,
        max_length=RESOURCE_LICENSE_DECISION_REASON_MAX,
    )

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < RESOURCE_LICENSE_DECISION_REASON_MIN:
            raise ValueError(
                f"O motivo deve ter entre {RESOURCE_LICENSE_DECISION_REASON_MIN} e "
                f"{RESOURCE_LICENSE_DECISION_REASON_MAX} caracteres."
            )
        return stripped


class ResourceLicenseDecisionResponse(CamelModel):
    id: str
    decision: ResourceLicenseDecisionKind
    reason: str
    actor_professional_id: str | None = None
    created_at: datetime


class ResourceLicenseSummary(CamelModel):
    """Licença corrente embutida em ``ResourceResponse`` (sem dados de curadoria)."""

    id: str
    version: int
    status: ResourceLicenseStatus
    origin: ResourceLicenseOrigin
    rights_holder: str
    attribution: str = ""
    valid_until: date | None = None
    allow_professional_distribution: bool = False
    allow_family_delivery: bool = False


class ResourceLicenseResponse(CamelModel):
    id: str
    resource_id: str
    version: int
    status: ResourceLicenseStatus
    origin: ResourceLicenseOrigin
    rights_holder: str
    source_reference: str | None = None
    evidence_reference: str | None = None
    attribution: str = ""
    valid_until: date | None = None
    allow_professional_distribution: bool = False
    allow_family_delivery: bool = False
    content_sha256: str | None = None
    declared_by_professional_id: str | None = None
    declared_by_admin: bool = False
    created_at: datetime
    updated_at: datetime
    decisions: list[ResourceLicenseDecisionResponse] = Field(default_factory=list)
