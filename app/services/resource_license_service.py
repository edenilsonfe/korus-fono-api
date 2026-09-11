"""F17 — serviço de licença de distribuição de recursos.

Uma licença fica ligada ao hash do arquivo verificado no armazenamento no
momento da declaração; expiração é derivada de ``valid_until`` (sem job de
aprovação automática). ``declared`` (autoral privado) habilita entrega
familiar apenas quando expressamente autorizado; distribuição profissional
exige ``approved``. As funções ``assert_can_publish`` e
``assert_can_deliver_to_family`` são as interfaces de gate consumidas pelo
resto do produto (F16) — ``canDeliverToFamily`` no DTO é só conveniência de UI.
"""

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.resource_catalog import RESOURCE_MAX_BYTES
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense, ResourceLicenseDecision
from app.schemas.resource_license import (
    ResourceLicenseDecisionCreate,
    ResourceLicenseDecisionResponse,
    ResourceLicenseDeclaration,
    ResourceLicenseResponse,
    ResourceLicenseSummary,
)
from app.services.storage import StorageLimitExceededError, storage_service

logger = logging.getLogger(__name__)

ARCHIVED_REASON = "Material arquivado; não está disponível para distribuição."
NO_LICENSE_REASON = "Material sem declaração de licença; distribuição não autorizada."
LICENSE_STATUS_REASONS: dict[str, str] = {
    "declared": "Licença de uso privado aguardando aprovação da curadoria.",
    "pending": "Licença aguardando revisão da curadoria.",
    "rejected": "Licença rejeitada pela curadoria.",
    "revoked": "Licença revogada.",
}
LICENSE_STATUS_LABELS: dict[str, str] = {
    "declared": "declarada",
    "pending": "pendente",
    "approved": "aprovada",
    "rejected": "rejeitada",
    "revoked": "revogada",
}
EXPIRED_REASON = "Licença expirada."
NO_FAMILY_REASON = "Licença sem permissão de entrega à família."
NO_PROFESSIONAL_REASON = "Licença sem permissão de distribuição profissional."
HASH_MISMATCH_REASON = "Licença não corresponde ao arquivo atual do material."

# Transições de decisão aceitas por estado atual da licença.
ALLOWED_DECISIONS: dict[str, frozenset[str]] = {
    "declared": frozenset({"approved", "rejected", "revoked"}),
    "pending": frozenset({"approved", "rejected"}),
    "approved": frozenset({"revoked"}),
    "rejected": frozenset(),
    "revoked": frozenset(),
}

_MISSING_OBJECT_CODES = {"NoSuchKey", "NoSuchBucket", "NotFound", "404"}


class ResourceLicenseNotFoundError(Exception):
    """404 — recurso/licença inexistente ou fora do escopo do ator."""

    def __init__(self, detail: str = "Recurso não encontrado") -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLicensePolicyError(Exception):
    """409 — estado/licença incompatíveis com a operação."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLicenseValidationError(Exception):
    """422 — declaração/decisão incoerente."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceLicenseStorageUnavailableError(Exception):
    """503 — storage indisponível para verificar o arquivo."""

    def __init__(self, detail: str = "Armazenamento indisponível no momento. Tente novamente.") -> None:
        super().__init__(detail)
        self.detail = detail


# Erros de licença mapeáveis para HTTP — routers usam `except LICENSE_ERRORS`.
LICENSE_ERRORS = (
    ResourceLicenseNotFoundError,
    ResourceLicensePolicyError,
    ResourceLicenseValidationError,
    ResourceLicenseStorageUnavailableError,
)


def license_http_error(exc: Exception) -> HTTPException:
    """Converte erros de licença nos HTTPException do contrato."""
    if isinstance(exc, ResourceLicenseNotFoundError):
        return HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=exc.detail)
    if isinstance(exc, ResourceLicensePolicyError):
        return HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=exc.detail)
    if isinstance(exc, ResourceLicenseValidationError):
        return HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.detail
        )
    if isinstance(exc, ResourceLicenseStorageUnavailableError):
        return HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail)
    raise exc


def sha256_hexdigest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def license_today() -> date:
    """Data local da clínica — expiração de licença não usa UTC do servidor."""
    tz_name = get_settings().clinic_timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - timezone inválida não pode derrubar o gate
        tz = timezone.utc
    return datetime.now(tz).date()


def license_is_expired(license: ResourceLicense, today: date | None = None) -> bool:
    if license.valid_until is None:
        return False
    return license.valid_until < (today or license_today())


def license_matches_content(license: ResourceLicense, resource: Resource) -> bool:
    """A licença só vale para o hash de conteúdo efetivamente verificado."""
    if not license.content_sha256 or not resource.content_sha256:
        return False
    return license.content_sha256 == resource.content_sha256


def license_is_valid_for_professionals(
    license: ResourceLicense | None, resource: Resource, *, today: date | None = None
) -> bool:
    """Licença que autoriza distribuição profissional (e visibilidade no catálogo)."""
    if license is None or license.status != "approved":
        return False
    if license_is_expired(license, today):
        return False
    if not license.allow_professional_distribution:
        return False
    return license_matches_content(license, resource)


def assert_can_publish(
    resource: Resource, license: ResourceLicense | None, *, today: date | None = None
) -> None:
    """Levanta ``ResourceLicensePolicyError`` quando o recurso não pode ser publicado."""
    if resource.publication_status == "archived":
        raise ResourceLicensePolicyError(ARCHIVED_REASON)
    if license is None:
        raise ResourceLicensePolicyError(NO_LICENSE_REASON)
    if license.status != "approved":
        raise ResourceLicensePolicyError(
            LICENSE_STATUS_REASONS.get(license.status, NO_LICENSE_REASON)
        )
    if license_is_expired(license, today):
        raise ResourceLicensePolicyError(EXPIRED_REASON)
    if not license_matches_content(license, resource):
        raise ResourceLicensePolicyError(HASH_MISMATCH_REASON)
    if not license.allow_professional_distribution:
        raise ResourceLicensePolicyError(NO_PROFESSIONAL_REASON)


def assert_can_deliver_to_family(
    resource: Resource, license: ResourceLicense | None, *, today: date | None = None
) -> None:
    """Levanta ``ResourceLicensePolicyError`` quando a entrega familiar é bloqueada."""
    if resource.publication_status == "archived":
        raise ResourceLicensePolicyError(ARCHIVED_REASON)
    if license is None:
        raise ResourceLicensePolicyError(NO_LICENSE_REASON)
    if license.status not in ("declared", "approved"):
        raise ResourceLicensePolicyError(
            LICENSE_STATUS_REASONS.get(license.status, NO_LICENSE_REASON)
        )
    if license_is_expired(license, today):
        raise ResourceLicensePolicyError(EXPIRED_REASON)
    if not license_matches_content(license, resource):
        raise ResourceLicensePolicyError(HASH_MISMATCH_REASON)
    if not license.allow_family_delivery:
        raise ResourceLicensePolicyError(NO_FAMILY_REASON)


def evaluate_family_delivery(
    resource: Resource, license: ResourceLicense | None, *, today: date | None = None
) -> tuple[bool, str | None]:
    """Conveniência de UI (``canDeliverToFamily``/``unavailableReason``).

    Nunca substitui a revalidação no servidor — quem entrega bytes chama
    ``assert_can_deliver_to_family``.
    """
    try:
        assert_can_deliver_to_family(resource, license, today=today)
    except ResourceLicensePolicyError as exc:
        return False, exc.detail
    return True, None


def _is_missing_object_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code", ""))
        if code in _MISSING_OBJECT_CODES:
            return True
        http_code = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", ""))
        return http_code == "404"
    return False


async def verify_resource_content(resource: Resource) -> str:
    """Verifica existência no storage e devolve o sha256 real do arquivo.

    ``ResourceLicensePolicyError`` (409) para objeto ausente/ilegível;
    ``ResourceLicenseStorageUnavailableError`` (503) quando o storage falha.
    """
    try:
        body, _ = await storage_service.download_limited(
            resource.storage_key, max_bytes=RESOURCE_MAX_BYTES
        )
    except StorageLimitExceededError as exc:
        raise ResourceLicensePolicyError(
            "Arquivo do material excede o limite permitido e não pôde ser verificado."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - mapeado para 409/503 conforme a causa
        if _is_missing_object_error(exc):
            raise ResourceLicensePolicyError(
                "Arquivo do material indisponível no armazenamento."
            ) from exc
        logger.exception("Falha ao verificar o arquivo do recurso %s", resource.id)
        raise ResourceLicenseStorageUnavailableError() from exc
    return sha256_hexdigest(body)


async def get_current_license(
    db: AsyncSession, resource_id: uuid.UUID
) -> ResourceLicense | None:
    result = await db.execute(
        select(ResourceLicense)
        .where(ResourceLicense.resource_id == resource_id)
        .order_by(ResourceLicense.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def current_licenses_by_resource(
    db: AsyncSession, resource_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ResourceLicense]:
    """Versão vigente (maior ``version``) por recurso, em uma consulta."""
    if not resource_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(ResourceLicense)
                .where(ResourceLicense.resource_id.in_(resource_ids))
                .order_by(ResourceLicense.resource_id, ResourceLicense.version)
            )
        )
        .scalars()
        .all()
    )
    current: dict[uuid.UUID, ResourceLicense] = {}
    for row in rows:
        current[row.resource_id] = row
    return current


async def decisions_for(db: AsyncSession, license_id: uuid.UUID) -> list[ResourceLicenseDecision]:
    rows = (
        (
            await db.execute(
                select(ResourceLicenseDecision)
                .where(ResourceLicenseDecision.license_id == license_id)
                .order_by(ResourceLicenseDecision.created_at, ResourceLicenseDecision.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def to_license_summary(license: ResourceLicense | None) -> ResourceLicenseSummary | None:
    """Resumo para ``ResourceResponse`` — sem comprovação administrativa."""
    if license is None:
        return None
    return ResourceLicenseSummary(
        id=str(license.id),
        version=license.version,
        status=license.status,  # type: ignore[arg-type]
        origin=license.origin,  # type: ignore[arg-type]
        rights_holder=license.rights_holder,
        attribution=license.attribution,
        valid_until=license.valid_until,
        allow_professional_distribution=license.allow_professional_distribution,
        allow_family_delivery=license.allow_family_delivery,
    )


def to_license_response(
    license: ResourceLicense, decisions: list[ResourceLicenseDecision] | None = None
) -> ResourceLicenseResponse:
    return ResourceLicenseResponse(
        id=str(license.id),
        resource_id=str(license.resource_id),
        version=license.version,
        status=license.status,  # type: ignore[arg-type]
        origin=license.origin,  # type: ignore[arg-type]
        rights_holder=license.rights_holder,
        source_reference=license.source_reference,
        evidence_reference=license.evidence_reference,
        attribution=license.attribution,
        valid_until=license.valid_until,
        allow_professional_distribution=license.allow_professional_distribution,
        allow_family_delivery=license.allow_family_delivery,
        content_sha256=license.content_sha256,
        declared_by_professional_id=(
            str(license.declared_by_professional_id)
            if license.declared_by_professional_id
            else None
        ),
        declared_by_admin=license.declared_by_admin,
        created_at=license.created_at,
        updated_at=license.updated_at,
        decisions=[
            ResourceLicenseDecisionResponse(
                id=str(decision.id),
                decision=decision.decision,  # type: ignore[arg-type]
                reason=decision.reason,
                actor_professional_id=(
                    str(decision.actor_professional_id)
                    if decision.actor_professional_id
                    else None
                ),
                created_at=decision.created_at,
            )
            for decision in (decisions or [])
        ],
    )


class ResourceLicenseService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_for_owner(
        self, professional: Professional, resource_id: uuid.UUID
    ) -> tuple[Resource, ResourceLicense | None]:
        """Recurso do dono + licença vigente. Fora do escopo devolve 404."""
        resource = await self.db.get(Resource, resource_id)
        if resource is None or resource.owner_professional_id != professional.id:
            raise ResourceLicenseNotFoundError()
        return resource, await get_current_license(self.db, resource_id)

    async def get_for_admin(
        self, resource_id: uuid.UUID
    ) -> tuple[Resource, ResourceLicense | None]:
        resource = await self.db.get(Resource, resource_id)
        if resource is None:
            raise ResourceLicenseNotFoundError()
        return resource, await get_current_license(self.db, resource_id)

    def _validate_declaration(
        self,
        resource: Resource,
        body: ResourceLicenseDeclaration,
        current: ResourceLicense | None,
    ) -> None:
        if resource.publication_status == "archived":
            raise ResourceLicensePolicyError(
                "Recurso arquivado não pode receber novas declarações de licença."
            )
        if body.valid_until is not None and body.valid_until < license_today():
            raise ResourceLicenseValidationError("Data de validade da licença já expirada.")
        if body.expected_version is not None and (
            current is None or current.version != body.expected_version
        ):
            raise ResourceLicensePolicyError(
                "Licença desatualizada. Recarregue o estado atual antes de declarar novamente."
            )

    async def _create_license(
        self,
        resource: Resource,
        body: ResourceLicenseDeclaration,
        *,
        license_status: str,
        actor: Professional,
        declared_by_admin: bool,
        current: ResourceLicense | None,
    ) -> ResourceLicense:
        content_sha256 = await verify_resource_content(resource)
        resource.content_sha256 = content_sha256
        license = ResourceLicense(
            resource_id=resource.id,
            version=(current.version + 1) if current is not None else 1,
            status=license_status,
            origin=body.origin,
            rights_holder=body.rights_holder,
            source_reference=body.source_reference,
            evidence_reference=body.evidence_reference,
            attribution=body.attribution,
            valid_until=body.valid_until,
            allow_professional_distribution=body.allow_professional_distribution,
            allow_family_delivery=body.allow_family_delivery,
            content_sha256=content_sha256,
            declared_by_professional_id=actor.id,
            declared_by_admin=declared_by_admin,
        )
        self.db.add(license)
        await self.db.flush()
        return license

    async def declare_personal(
        self, professional: Professional, resource_id: uuid.UUID, body: ResourceLicenseDeclaration
    ) -> ResourceLicense:
        """Declaração do dono: autoral vira ``declared``; terceiros vão a ``pending``."""
        resource, current = await self.get_for_owner(professional, resource_id)
        self._validate_declaration(resource, body, current)
        license_status = "declared" if body.origin == "original" else "pending"
        return await self._create_license(
            resource,
            body,
            license_status=license_status,
            actor=professional,
            declared_by_admin=False,
            current=current,
        )

    async def declare_global_admin(
        self, actor: Professional, resource_id: uuid.UUID, body: ResourceLicenseDeclaration
    ) -> ResourceLicense:
        """Curadoria declara em nome do titular de material global — sempre ``pending``."""
        resource, current = await self.get_for_admin(resource_id)
        if resource.owner_professional_id is not None:
            raise ResourceLicensePolicyError(
                "Material pessoal deve ser declarado pelo próprio dono."
            )
        self._validate_declaration(resource, body, current)
        if not body.evidence_reference:
            raise ResourceLicenseValidationError(
                "Declaração de material global exige comprovação do titular dos direitos."
            )
        return await self._create_license(
            resource,
            body,
            license_status="pending",
            actor=actor,
            declared_by_admin=True,
            current=current,
        )

    async def decide_admin(
        self, actor: Professional, resource_id: uuid.UUID, body: ResourceLicenseDecisionCreate
    ) -> tuple[Resource, ResourceLicense, ResourceLicenseDecision]:
        """Aprova/rejeita/revoga a licença vigente. Não altera a propriedade."""
        resource, current = await self.get_for_admin(resource_id)
        license = await self.db.get(ResourceLicense, body.license_id)
        if license is None or license.resource_id != resource.id:
            raise ResourceLicenseNotFoundError("Licença não encontrada")
        if current is None or license.id != current.id:
            raise ResourceLicensePolicyError(
                "Decisão permitida apenas para a versão vigente da licença."
            )
        allowed = ALLOWED_DECISIONS.get(license.status, frozenset())
        if body.decision not in allowed:
            label = LICENSE_STATUS_LABELS.get(license.status, license.status)
            raise ResourceLicensePolicyError(
                f"Licença {label} não aceita nova decisão de curadoria."
            )
        license.status = body.decision
        decision = ResourceLicenseDecision(
            license_id=license.id,
            decision=body.decision,
            reason=body.reason,
            actor_professional_id=actor.id,
        )
        self.db.add(decision)
        await self.db.flush()
        return resource, license, decision

    async def set_publication(self, resource_id: uuid.UUID, body) -> Resource:
        """Publica/arquiva o recurso; publicar exige licença aprovada e blob verificado."""
        resource = await self.db.get(Resource, resource_id)
        if resource is None:
            raise ResourceLicenseNotFoundError()

        if body.status == "archived":
            if resource.publication_status == "archived":
                return resource
            resource.publication_status = "archived"
            resource.archived_at = datetime.now(UTC)
            await self.db.flush()
            return resource

        if resource.publication_status == "archived":
            raise ResourceLicensePolicyError(
                "Recurso arquivado não pode ser republicado."
            )
        current = await get_current_license(self.db, resource.id)
        assert_can_publish(resource, current, today=license_today())
        content_sha256 = await verify_resource_content(resource)
        if resource.content_sha256 is None:
            resource.content_sha256 = content_sha256
        elif content_sha256 != resource.content_sha256:
            raise ResourceLicensePolicyError(
                "Arquivo do material difere do conteúdo verificado na licença."
            )
        resource.publication_status = "published"
        await self.db.flush()
        return resource
