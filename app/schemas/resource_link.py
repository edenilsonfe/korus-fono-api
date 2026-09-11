"""F17/4.2 — schemas de vínculos de recursos (camelCase, extra=forbid)."""

from pydantic import ConfigDict

from app.schemas.common import CamelModel
from app.schemas.resource import ResourceResponse


class ResourceDomainsUpdate(CamelModel):
    """PUT de domínios: substitui o conjunto inteiro do recurso."""

    model_config = ConfigDict(extra="forbid")

    domain_keys: list[str] = []


class LinkResourceBody(CamelModel):
    """Body vazio dos PUT de vínculo — replay idempotente por par de FKs."""

    model_config = ConfigDict(extra="forbid")


class LinkedResourceResponse(CamelModel):
    """Recurso vinculado a meta/programa visto pelo profissional atual.

    Sem ACL, o material de colega não expõe título clínico nem DTO/arquivo:
    ``available=false`` com motivo e ``resource=None``.
    """

    resource_id: str
    title: str
    available: bool
    reason: str | None = None
    resource: ResourceResponse | None = None
