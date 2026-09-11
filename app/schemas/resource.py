from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.resource_catalog import (
    RESOURCE_ACCENTS,
    RESOURCE_CATEGORIES,
    RESOURCE_DIFFICULTIES,
    RESOURCE_FORMATS,
)
from app.schemas.common import CamelModel
from app.schemas.resource_license import ResourceLicenseSummary

ResourceScope = Literal["all", "global", "mine"]
ResourceFormat = Literal["PDF", "Imagem"]
ResourceDifficulty = Literal["Básico", "Intermediário", "Avançado"]
ResourceAccent = Literal["primary", "info", "success", "warning", "destructive"]
ResourcePublicationStatus = Literal["draft", "published", "archived"]


def _validate_categories(value: list[str]) -> list[str]:
    invalid = [c for c in value if c not in RESOURCE_CATEGORIES]
    if invalid:
        raise ValueError(f"Categorias inválidas: {', '.join(invalid)}")
    return value


class ResourceResponse(CamelModel):
    id: str
    title: str
    description: str
    categories: list[str]
    format: ResourceFormat
    file_size_bytes: int
    pages: int | None = None
    author: str
    updated_at: datetime
    downloads: int
    featured: bool = False
    accent: ResourceAccent = "primary"
    objective: str | None = None
    age_range: str | None = None
    skill: str | None = None
    related_protocol: str | None = None
    difficulty: ResourceDifficulty | None = None
    is_mine: bool = False
    shared_with_platform: bool = False
    # F17 — estado editorial/licença. Nunca expor comprovação administrativa,
    # storage_key ou os pacientes vinculados.
    publication_status: ResourcePublicationStatus = "draft"
    # Vínculos canônicos de domínio (ResourceDomainLink, tarefa 4.2) — somente
    # leitura aqui; vínculo não amplia a visibilidade/ACL do recurso.
    domain_keys: list[str] = Field(default_factory=list)
    license: ResourceLicenseSummary | None = None
    content_sha256: str | None = None
    can_deliver_to_family: bool = False
    unavailable_reason: str | None = None


class ResourceDownloadUrl(CamelModel):
    url: str


class ResourceCreateBody(CamelModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    categories: list[str] = Field(default_factory=list)
    pages: int | None = None
    author: str | None = None
    accent: ResourceAccent = "primary"
    objective: str | None = None
    age_range: str | None = None
    skill: str | None = None
    related_protocol: str | None = None
    difficulty: ResourceDifficulty | None = None
    shared_with_platform: bool = False

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        return _validate_categories(value)


class ResourceUpdateBody(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    categories: list[str] | None = None
    pages: int | None = None
    author: str | None = None
    accent: ResourceAccent | None = None
    objective: str | None = None
    age_range: str | None = None
    skill: str | None = None
    related_protocol: str | None = None
    difficulty: ResourceDifficulty | None = None
    shared_with_platform: bool | None = None

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return _validate_categories(value)


class AdminResourceCreateBody(ResourceCreateBody):
    featured: bool = False


class AdminResourceUpdateBody(ResourceUpdateBody):
    featured: bool | None = None
