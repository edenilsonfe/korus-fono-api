import json
import uuid
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resource_catalog import (
    RESOURCE_ALLOWED_CONTENT_TYPES,
    RESOURCE_MAX_BYTES,
)
from app.models.professional import Professional
from app.models.resource import Resource
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
from app.services.storage import storage_service

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024


class ResourceNotFoundError(Exception):
    pass


class ResourceForbiddenError(Exception):
    pass


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


def to_resource_response(resource: Resource, professional_id: uuid.UUID) -> ResourceResponse:
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
        is_mine=resource.owner_professional_id == professional_id,
        shared_with_platform=resource.shared_with_platform,
    )


def _apply_metadata(resource: Resource, payload: dict[str, Any]) -> None:
    for field, value in payload.items():
        if value is not None:
            setattr(resource, field, value)


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
    ) -> list[Resource]:
        stmt = select(Resource)
        if scope == "global":
            stmt = stmt.where(
                or_(
                    Resource.owner_professional_id.is_(None),
                    Resource.shared_with_platform.is_(True),
                )
            )
        elif scope == "mine":
            stmt = stmt.where(Resource.owner_professional_id == professional.id)
        else:
            stmt = stmt.where(
                or_(
                    Resource.owner_professional_id.is_(None),
                    Resource.owner_professional_id == professional.id,
                    Resource.shared_with_platform.is_(True),
                )
            )

        result = await self.db.execute(stmt.order_by(Resource.updated_at.desc()))
        items = list(result.scalars().all())

        if category:
            items = [
                item
                for item in items
                if category in (item.categories or [])
            ]

        if q:
            needle = q.casefold()
            items = [
                item
                for item in items
                if needle in item.title.casefold()
                or needle in item.description.casefold()
                or any(needle in cat.casefold() for cat in (item.categories or []))
            ]
        return items

    async def list_global_admin(self) -> list[Resource]:
        result = await self.db.execute(
            select(Resource)
            .where(Resource.owner_professional_id.is_(None))
            .order_by(Resource.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, resource_id: uuid.UUID) -> Resource | None:
        return await self.db.get(Resource, resource_id)

    async def get_accessible(self, professional: Professional, resource_id: uuid.UUID) -> Resource:
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id in (None, professional.id):
            return resource
        if resource.shared_with_platform:
            return resource
        raise ResourceForbiddenError()

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
        )
        self.db.add(resource)
        await self.db.flush()
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
            upload_body = await read_upload_body(file)
            content_type, resource_format, filename = validate_resource_upload(
                content_type=file.content_type,
                filename=file.filename,
                body=upload_body,
            )
            storage_key = storage_service.make_resource_key(resource.id, filename)
            await storage_service.upload(storage_key, upload_body, content_type)
            resource.storage_key = storage_key
            resource.content_type = content_type
            resource.format = resource_format
            resource.file_size_bytes = len(upload_body)

        _apply_metadata(resource, payload.model_dump(exclude_unset=True))
        await self.db.flush()
        return resource

    async def delete_personal(self, professional: Professional, resource_id: uuid.UUID) -> None:
        resource = await self.get_by_id(resource_id)
        if resource is None:
            raise ResourceNotFoundError()
        if resource.owner_professional_id != professional.id:
            raise ResourceForbiddenError()
        await self.db.delete(resource)

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
        )
        self.db.add(resource)
        await self.db.flush()
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
            upload_body = await read_upload_body(file)
            content_type, resource_format, filename = validate_resource_upload(
                content_type=file.content_type,
                filename=file.filename,
                body=upload_body,
            )
            storage_key = storage_service.make_resource_key(resource.id, filename)
            await storage_service.upload(storage_key, upload_body, content_type)
            resource.storage_key = storage_key
            resource.content_type = content_type
            resource.format = resource_format
            resource.file_size_bytes = len(upload_body)

        _apply_metadata(resource, payload.model_dump(exclude_unset=True))
        await self.db.flush()
        return resource

    async def delete_global_admin(self, resource_id: uuid.UUID) -> None:
        resource = await self.get_by_id(resource_id)
        if resource is None or resource.owner_professional_id is not None:
            raise ResourceNotFoundError()
        await self.db.delete(resource)

    async def download_url(self, professional: Professional, resource_id: uuid.UUID) -> str:
        resource = await self.get_accessible(professional, resource_id)
        resource.downloads += 1
        await self.db.flush()
        return await storage_service.presigned_url(
            resource.storage_key,
            filename=resource.title,
        )

    async def download_content(
        self, professional: Professional, resource_id: uuid.UUID
    ) -> tuple[bytes, Resource]:
        """Fetch object bytes same-origin (inline preview) and bump the download counter."""
        resource = await self.get_accessible(professional, resource_id)
        resource.downloads += 1
        await self.db.flush()
        body, _ = await storage_service.download(resource.storage_key)
        return body, resource
