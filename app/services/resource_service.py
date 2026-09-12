import hashlib
import json
import uuid
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resource_catalog import (
    RESOURCE_ALLOWED_CONTENT_TYPES,
    RESOURCE_MAX_BYTES,
)
from app.core.utils import utcnow
from app.models.family_portal_content import FamilyPortalItem
from app.models.home_program import HomeProgramTaskResource
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense, ResourceLicenseDecision
from app.models.resource_link import (
    GoalResourceLink,
    ProgramResourceLink,
    ResourceDomainLink,
)
from app.schemas.resource import (
    AdminResourceCreateBody,
    AdminResourceUpdateBody,
    ResourceCreateBody,
    ResourceResponse,
    ResourceScope,
    ResourceUpdateBody,
)
from app.services.attachment_upload import (
    assert_declared_matches_sniff,
    normalize_content_type,
    sanitize_filename,
)
from app.services.resource_license_service import (
    current_licenses_by_resource,
    evaluate_family_delivery,
    get_current_license,
    license_is_valid_for_professionals,
    to_license_summary,
)
from app.services.resource_link_service import domain_keys_by_resource
from app.services.storage import storage_service
from app.services.storage_cleanup_service import (
    queue_storage_cleanup,
    reserve_storage_cleanup,
    resolve_storage_cleanup,
)

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024

# Campos opcionais que o multipart pode limpar explicitamente (JSON array camelCase).
RESOURCE_CLEARABLE_FIELDS: dict[str, str] = {
    "objective": "objective",
    "ageRange": "age_range",
    "skill": "skill",
    "relatedProtocol": "related_protocol",
    "difficulty": "difficulty",
    "pages": "pages",
}


class ResourceNotFoundError(Exception):
    pass


class ResourceForbiddenError(Exception):
    pass


class ResourceHasReferencesError(Exception):
    """409 — recurso com referências/estado editorial não aceita a operação.

    Substituição (arquivo referenciado por vínculo) e exclusão (vínculo,
    licença ou entrega) exigem nova versão/arquivamento, nunca sobrescrita
    destrutiva.
    """

    def __init__(self, detail: str = "Cadastre uma nova versão do material.") -> None:
        super().__init__(detail)
        self.detail = detail


# Detalhes user-facing (pt-BR) para cada operação bloqueada por referências.
REPLACE_REFERENCED_DETAIL = "Cadastre uma nova versão do material."
DELETE_REFERENCED_DETAIL = (
    "Recurso referenciado não pode ser excluído; arquive o material para "
    "preservar o histórico."
)


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


async def read_upload_body(file: UploadFile, max_bytes: int = RESOURCE_MAX_BYTES) -> bytes:
    chunks: list[bytes] = []
    total_read = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Arquivo excede o tamanho máximo permitido de {_format_size_bytes(max_bytes)}."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def validate_content_type(content_type: str | None) -> tuple[str, str]:
    normalized = normalize_content_type(content_type)
    resource_format = RESOURCE_ALLOWED_CONTENT_TYPES.get(normalized)
    if resource_format is None:
        allowed = ", ".join(sorted(RESOURCE_ALLOWED_CONTENT_TYPES))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de arquivo não suportado. Permitidos: {allowed}.",
        )
    return normalized, resource_format


def validate_resource_upload(
    *,
    content_type: str | None,
    filename: str | None,
    body: bytes,
) -> tuple[str, str, str]:
    """Return (normalized_content_type, resource_format, safe_filename)."""
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Arquivo vazio não é permitido.",
        )
    normalized, resource_format = validate_content_type(content_type)
    assert_declared_matches_sniff(normalized, body)
    return normalized, resource_format, sanitize_filename(filename or "material")


def parse_categories_form(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Campo categories deve ser um JSON array de strings.",
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Campo categories deve ser um JSON array de strings.",
        )
    return parsed


def parse_clear_fields_form(raw: str | None) -> set[str]:
    """Lê ``clearFields`` (JSON array camelCase) — limpar difere de omitir."""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Campo clearFields deve ser um JSON array de nomes de campos.",
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Campo clearFields deve ser um JSON array de nomes de campos.",
        )
    unknown = sorted({name for name in parsed if name not in RESOURCE_CLEARABLE_FIELDS})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Campo não pode ser limpo: {', '.join(unknown)}.",
        )
    return set(parsed)


def resolve_compat_field(
    *, value: str | None, legacy: str | None, name: str, legacy_name: str
) -> str | None:
    """Nome novo (camelCase) com compatibilidade temporária do nome antigo."""
    if value is not None and legacy is not None and value != legacy:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Informe {name} ou {legacy_name}, não ambos com valores diferentes.",
        )
    return value if value is not None else legacy


def resolve_compat_flag(
    *, value: bool | None, legacy: bool | None, name: str, legacy_name: str
) -> bool | None:
    if value is not None and legacy is not None and value != legacy:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Informe {name} ou {legacy_name}, não ambos com valores diferentes.",
        )
    return value if value is not None else legacy


def apply_clear_fields(
    payload: dict[str, Any], clear_fields: set[str], provided: dict[str, Any]
) -> None:
    """Adiciona ``None`` explícito para os campos limpos e rejeita contradição."""
    for name in sorted(clear_fields):
        value = provided.get(name)
        has_value = value is not None and (not isinstance(value, str) or value.strip() != "")
        if has_value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Campo {name} não pode ser limpo e receber valor na mesma requisição."
                ),
            )
        payload[RESOURCE_CLEARABLE_FIELDS[name]] = None


async def _resource_has_link(db: AsyncSession, link_model, resource_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(link_model.id).where(link_model.resource_id == resource_id).limit(1)
    )
    return found is not None


async def resource_has_references(
    db: AsyncSession,
    resource: Resource,
    *,
    include_licenses: bool = False,
    include_domain_links: bool = False,
) -> bool:
    """Referências vivas que impedem substituir/apagar o arquivo no lugar.

    Publicação/arquivamento, vínculos de meta/programa (prescrição — 4.2),
    materiais prescritos em tarefa de programa de casa (F16 — 5.3) e itens do
    portal da família (F14 — inclusive retirados, cujo histórico congelou o
    hash) sempre bloqueiam: o arquivo entregue à família não pode mudar por
    baixo do vínculo. Licenças e vínculos de domínio entram apenas na exclusão
    (preservam histórico editorial); na substituição a licença anterior fica
    presa ao hash antigo e deixa de valer (aprovação retirada por
    incompatibilidade de conteúdo). Novo conteúdo = novo ``Resource`` (ver
    §3.4, F17).
    """
    if resource.publication_status in ("published", "archived"):
        return True
    if await _resource_has_link(db, GoalResourceLink, resource.id):
        return True
    if await _resource_has_link(db, ProgramResourceLink, resource.id):
        return True
    if await _resource_has_link(db, HomeProgramTaskResource, resource.id):
        return True
    if await _resource_has_link(db, FamilyPortalItem, resource.id):
        return True
    if include_domain_links and await _resource_has_link(db, ResourceDomainLink, resource.id):
        return True
    if include_licenses:
        license_id = await db.scalar(
            select(ResourceLicense.id)
            .where(ResourceLicense.resource_id == resource.id)
            .limit(1)
        )
        if license_id is not None:
            return True
    return False


def _apply_metadata(resource: Resource, payload: dict[str, Any]) -> None:
    """Aplica só o que veio no PATCH — inclusive ``None`` (limpeza explícita)."""
    for field, value in payload.items():
        setattr(resource, field, value)


def to_resource_response(
    resource: Resource,
    professional_id: uuid.UUID | None = None,
    *,
    license: ResourceLicense | None = None,
    domain_keys: list[str] | None = None,
) -> ResourceResponse:
    can_deliver, unavailable_reason = evaluate_family_delivery(resource, license)
    keys = domain_keys or []
    return ResourceResponse(
        id=str(resource.id),
        title=resource.title,
        description=resource.description,
        categories=resource.categories or [],
        format=resource.format,  # type: ignore[arg-type]
        file_size_bytes=resource.file_size_bytes,
        pages=resource.pages,
        author=resource.author,
        updated_at=resource.updated_at,
        downloads=resource.downloads,
        featured=resource.featured,
        accent=resource.accent,  # type: ignore[arg-type]
        objective=resource.objective,
        age_range=resource.age_range,
        skill=resource.skill,
        related_protocol=resource.related_protocol,
        difficulty=resource.difficulty,  # type: ignore[arg-type]
        is_mine=(
            professional_id is not None and resource.owner_professional_id == professional_id
        ),
        shared_with_platform=resource.shared_with_platform,
        publication_status=resource.publication_status,  # type: ignore[arg-type]
        domain_keys=list(keys),
        license=to_license_summary(license),
        content_sha256=resource.content_sha256,
        can_deliver_to_family=can_deliver,
        unavailable_reason=unavailable_reason,
    )


class ResourceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_professional(
        self,
        professional: Professional,
        *,
        q: str | None = None,
        category: str | None = None,
        scope: ResourceScope = "all",
        domain_key: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[tuple[Resource, ResourceLicense | None, list[str]]]:
        """Visibilidade: próprio OU publicado com licença válida para profissionais.

        Vínculo (meta/programa) não amplia visibilidade. ``domainKey`` filtra
        pelos vínculos persistidos (``ResourceDomainLink``). Ordenação estável
        por ``updatedAt``/``id`` e paginação aplicadas depois dos filtros.
        """
        stmt = select(Resource)
        if scope == "mine":
            stmt = stmt.where(Resource.owner_professional_id == professional.id)

        result = await self.db.execute(stmt.order_by(Resource.updated_at.desc(), Resource.id.desc()))
        candidates = list(result.scalars().all())
        licenses = await current_licenses_by_resource(self.db, [item.id for item in candidates])
        keys_by_resource = await domain_keys_by_resource(
            self.db, [item.id for item in candidates]
        )

        items: list[tuple[Resource, ResourceLicense | None]] = []
        for resource in candidates:
            is_own = resource.owner_professional_id == professional.id
            license = licenses.get(resource.id)
            if scope == "mine":
                if not is_own:
                    continue
            else:
                if not is_own and (
                    resource.publication_status != "published"
                    or not license_is_valid_for_professionals(license, resource)
                ):
                    continue
                if scope == "global" and is_own:
                    continue
            items.append((resource, license))

        if domain_key is not None:
            items = [pair for pair in items if domain_key in keys_by_resource.get(pair[0].id, [])]

        if category:
            items = [pair for pair in items if category in (pair[0].categories or [])]

        if q:
            needle = q.casefold()
            items = [
                pair
                for pair in items
                if needle in pair[0].title.casefold()
                or needle in pair[0].description.casefold()
                or any(needle in cat.casefold() for cat in (pair[0].categories or []))
            ]

        items.sort(key=lambda pair: (pair[0].updated_at, pair[0].id), reverse=True)
        return [
            (resource, license, keys_by_resource.get(resource.id, []))
            for resource, license in items[offset : offset + limit]
        ]

    async def list_for_admin(
        self,
        *,
        publication_status: str | None = None,
        license_status: str | None = None,
        include_submissions: bool = False,
        domain_key: str | None = None,
    ) -> list[tuple[Resource, ResourceLicense | None, list[str]]]:
        """Catálogo global; ``include_submissions`` inclui materiais pessoais submetidos."""
        stmt = select(Resource)
        if include_submissions:
            stmt = stmt.where(
                or_(
                    Resource.owner_professional_id.is_(None),
                    Resource.shared_with_platform.is_(True),
                )
            )
        else:
            stmt = stmt.where(Resource.owner_professional_id.is_(None))
        stmt = stmt.order_by(Resource.updated_at.desc(), Resource.id.desc())

        candidates = list((await self.db.execute(stmt)).scalars().all())
        licenses = await current_licenses_by_resource(self.db, [item.id for item in candidates])
        keys_by_resource = await domain_keys_by_resource(
            self.db, [item.id for item in candidates]
        )

        items: list[tuple[Resource, ResourceLicense | None]] = []
        for resource in candidates:
            license = licenses.get(resource.id)
            if publication_status is not None and resource.publication_status != publication_status:
                continue
            if license_status is not None and (
                license is None or license.status != license_status
            ):
                continue
            if domain_key is not None and domain_key not in keys_by_resource.get(resource.id, []):
                continue
            items.append((resource, license))
        return [
            (resource, license, keys_by_resource.get(resource.id, []))
            for resource, license in items
        ]

    async def get_by_id(self, resource_id: uuid.UUID) -> Resource | None:
        return await self.db.get(Resource, resource_id)

    async def get_accessible(self, professional: Professional, resource_id: uuid.UUID) -> Resource:
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id == professional.id:
            return resource
        license = await get_current_license(self.db, resource_id)
        if resource.publication_status == "published" and license_is_valid_for_professionals(
            license, resource
        ):
            return resource
        raise ResourceForbiddenError()

    async def _delete_licenses(self, resource_id: uuid.UUID) -> None:
        license_ids = select(ResourceLicense.id).where(ResourceLicense.resource_id == resource_id)
        await self.db.execute(
            delete(ResourceLicenseDecision).where(
                ResourceLicenseDecision.license_id.in_(license_ids)
            )
        )
        await self.db.execute(
            delete(ResourceLicense).where(ResourceLicense.resource_id == resource_id)
        )

    async def create_personal(
        self,
        professional: Professional,
        *,
        file: UploadFile,
        body: ResourceCreateBody,
    ) -> Resource:
        upload_body = await read_upload_body(file)
        content_type, resource_format, filename = validate_resource_upload(
            content_type=file.content_type,
            filename=file.filename,
            body=upload_body,
        )
        resource_id = uuid.uuid4()
        storage_key = storage_service.make_resource_key(resource_id, filename)
        # Reserva ANTES do upload (commit próprio): se o processo cair entre o
        # upload e a associação, o janitor remove o objeto órfão.
        reservation = await reserve_storage_cleanup(
            self.db,
            storage_key,
            reason="resource_create",
            professional_id=professional.id,
        )
        await storage_service.upload(storage_key, upload_body, content_type)

        resource = Resource(
            id=resource_id,
            owner_professional_id=professional.id,
            title=body.title,
            description=body.description,
            categories=body.categories,
            format=resource_format,
            file_size_bytes=len(upload_body),
            pages=body.pages,
            author=body.author or professional.name,
            storage_key=storage_key,
            content_type=content_type,
            accent=body.accent,
            objective=body.objective,
            age_range=body.age_range,
            skill=body.skill,
            related_protocol=body.related_protocol,
            difficulty=body.difficulty,
            featured=False,
            shared_with_platform=body.shared_with_platform,
            content_sha256=hashlib.sha256(upload_body).hexdigest(),
        )
        self.db.add(resource)
        await self.db.flush()
        # Retirada da reserva na MESMA transação que associa o blob.
        resolve_storage_cleanup(reservation)
        return resource

    async def update_personal(
        self,
        professional: Professional,
        resource_id: uuid.UUID,
        payload: ResourceUpdateBody,
        *,
        file: UploadFile | None = None,
    ) -> Resource:
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id != professional.id:
            raise ResourceForbiddenError()

        if file is not None:
            if await resource_has_references(self.db, resource):
                raise ResourceHasReferencesError()
            upload_body = await read_upload_body(file)
            content_type, resource_format, filename = validate_resource_upload(
                content_type=file.content_type,
                filename=file.filename,
                body=upload_body,
            )
            previous_key = resource.storage_key
            storage_key = storage_service.make_resource_key(resource.id, filename)
            reservation = await reserve_storage_cleanup(
                self.db,
                storage_key,
                reason="resource_replace",
                professional_id=professional.id,
            )
            await storage_service.upload(storage_key, upload_body, content_type)
            resource.storage_key = storage_key
            resource.content_type = content_type
            resource.format = resource_format
            resource.file_size_bytes = len(upload_body)
            # Novo conteúdo: a licença anterior fica presa ao hash antigo e a
            # aprovação/publicação anterior não sobrevive (novo material = novo
            # hash; republicar exige nova declaração).
            resource.content_sha256 = hashlib.sha256(upload_body).hexdigest()
            resolve_storage_cleanup(reservation)
            if previous_key and previous_key != storage_key:
                # O blob anterior só pode sumir depois do commit desta troca;
                # o janitor revalida que ninguém mais o referencia.
                queue_storage_cleanup(
                    self.db,
                    previous_key,
                    reason="resource_replace_previous",
                    professional_id=professional.id,
                )

        _apply_metadata(resource, payload.model_dump(exclude_unset=True))
        await self.db.flush()
        return resource

    async def delete_personal(self, professional: Professional, resource_id: uuid.UUID) -> None:
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id != professional.id:
            raise ResourceForbiddenError()
        await self._ensure_resource_not_referenced(resource)
        await self._delete_licenses(resource_id)
        key_to_clean = resource.storage_key
        await self.db.delete(resource)
        if key_to_clean:
            # Blob só pode sumir depois do commit do DELETE; o janitor revalida.
            queue_storage_cleanup(
                self.db,
                key_to_clean,
                reason="resource_delete",
                professional_id=professional.id,
            )

    async def create_global_admin(
        self,
        *,
        file: UploadFile,
        body: AdminResourceCreateBody,
    ) -> Resource:
        upload_body = await read_upload_body(file)
        content_type, resource_format, filename = validate_resource_upload(
            content_type=file.content_type,
            filename=file.filename,
            body=upload_body,
        )
        resource_id = uuid.uuid4()
        storage_key = storage_service.make_resource_key(resource_id, filename)
        reservation = await reserve_storage_cleanup(
            self.db,
            storage_key,
            reason="resource_create",
        )
        await storage_service.upload(storage_key, upload_body, content_type)

        resource = Resource(
            id=resource_id,
            owner_professional_id=None,
            title=body.title,
            description=body.description,
            categories=body.categories,
            format=resource_format,
            file_size_bytes=len(upload_body),
            pages=body.pages,
            author=body.author or "Equipe KorusFono",
            storage_key=storage_key,
            content_type=content_type,
            accent=body.accent,
            objective=body.objective,
            age_range=body.age_range,
            skill=body.skill,
            related_protocol=body.related_protocol,
            difficulty=body.difficulty,
            featured=body.featured,
            content_sha256=hashlib.sha256(upload_body).hexdigest(),
        )
        self.db.add(resource)
        await self.db.flush()
        resolve_storage_cleanup(reservation)
        return resource

    async def update_global_admin(
        self,
        resource_id: uuid.UUID,
        payload: AdminResourceUpdateBody,
        *,
        file: UploadFile | None = None,
    ) -> Resource:
        resource = await self.get_by_id(resource_id)
        if resource is None or resource.owner_professional_id is not None:
            raise ResourceNotFoundError()

        if file is not None:
            if await resource_has_references(self.db, resource):
                raise ResourceHasReferencesError()
            upload_body = await read_upload_body(file)
            content_type, resource_format, filename = validate_resource_upload(
                content_type=file.content_type,
                filename=file.filename,
                body=upload_body,
            )
            previous_key = resource.storage_key
            storage_key = storage_service.make_resource_key(resource.id, filename)
            reservation = await reserve_storage_cleanup(
                self.db, storage_key, reason="resource_replace"
            )
            await storage_service.upload(storage_key, upload_body, content_type)
            resource.storage_key = storage_key
            resource.content_type = content_type
            resource.format = resource_format
            resource.file_size_bytes = len(upload_body)
            resource.content_sha256 = hashlib.sha256(upload_body).hexdigest()
            resolve_storage_cleanup(reservation)
            if previous_key and previous_key != storage_key:
                queue_storage_cleanup(
                    self.db, previous_key, reason="resource_replace_previous"
                )

        _apply_metadata(resource, payload.model_dump(exclude_unset=True))
        await self.db.flush()
        return resource

    async def delete_global_admin(self, resource_id: uuid.UUID) -> None:
        resource = await self.get_by_id(resource_id)
        if resource is None or resource.owner_professional_id is not None:
            raise ResourceNotFoundError()
        await self._ensure_resource_not_referenced(resource)
        await self._delete_licenses(resource_id)
        key_to_clean = resource.storage_key
        await self.db.delete(resource)
        if key_to_clean:
            queue_storage_cleanup(self.db, key_to_clean, reason="resource_delete")

    async def _ensure_resource_not_referenced(self, resource: Resource) -> None:
        """409 para vínculo/licença/estado editorial: arquivar preserva histórico."""
        if await resource_has_references(
            self.db,
            resource,
            include_licenses=True,
            include_domain_links=True,
        ):
            raise ResourceHasReferencesError(DELETE_REFERENCED_DETAIL)

    async def archive_personal(
        self, professional: Professional, resource_id: uuid.UUID
    ) -> Resource:
        """Arquiva o próprio material — idempotente, preserva o histórico.

        Bloqueia novas distribuições/prescrições (os gates de licença e de
        vínculo revalidam ``publication_status == "archived"``) e revoga a
        disponibilização externa. Não apaga arquivo, licenças nem vínculos:
        republicar material arquivado exige um novo ``Resource``.
        """
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id != professional.id:
            raise ResourceForbiddenError()
        if resource.publication_status != "archived":
            resource.publication_status = "archived"
            resource.archived_at = utcnow()
            await self.db.flush()
        return resource

    async def download_url(self, professional: Professional, resource_id: uuid.UUID) -> str:
        """Emite URL assinada e incrementa a métrica por UPDATE atômico.

        A métrica é "solicitações de download" (não downloads concluídos) — o
        incremento em SQL evita perder contagens em emissões concorrentes.
        """
        resource = await self.get_accessible(professional, resource_id)
        await self.db.execute(
            update(Resource)
            .where(Resource.id == resource.id)
            .values(downloads=Resource.downloads + 1)
        )
        return await storage_service.presigned_url(
            resource.storage_key,
            filename=resource.title,
        )

    async def download_content(
        self, professional: Professional, resource_id: uuid.UUID
    ) -> tuple[bytes, Resource]:
        """Fetch object bytes same-origin (inline preview).

        Preview NÃO incrementa a métrica de solicitações de download — só a
        emissão de ``download-url`` conta.
        """
        resource = await self.get_accessible(professional, resource_id)
        body, _ = await storage_service.download(resource.storage_key)
        return body, resource
