"""Admin endpoints for the global resources catalog (platform staff only)."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_PRODUCT_READ, PERMISSION_PRODUCT_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.resource import (
    AdminResourceCreateBody,
    AdminResourceUpdateBody,
    ResourceResponse,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.resource_service import (
    ResourceNotFoundError,
    ResourceService,
    parse_categories_form,
)

router = APIRouter(prefix="/admin/resources", tags=["admin-resources"])


def _to_admin_response(resource) -> ResourceResponse:
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
        is_mine=False,
        shared_with_platform=resource.shared_with_platform,
    )


@router.get("", response_model=list[ResourceResponse])
@router.get("/", response_model=list[ResourceResponse])
async def list_admin_resources(
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = ResourceService(db)
    items = await service.list_global_admin()
    return [_to_admin_response(item) for item in items]


@router.post("", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_resource(
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(""),
    categories: str = Form("[]"),
    pages: int | None = Form(None),
    author: str | None = Form(None),
    accent: str = Form("primary"),
    objective: str | None = Form(None),
    age_range: str | None = Form(None),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    difficulty: str | None = Form(None),
    featured: bool = Form(False),
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    body = AdminResourceCreateBody(
        title=title,
        description=description,
        categories=parse_categories_form(categories),
        pages=pages,
        author=author,
        accent=accent,  # type: ignore[arg-type]
        objective=objective,
        age_range=age_range,
        skill=skill,
        related_protocol=related_protocol,
        difficulty=difficulty,  # type: ignore[arg-type]
        featured=featured,
    )
    service = ResourceService(db)
    resource = await service.create_global_admin(file=file, body=body)
    await AdminAuditService(db).log(
        actor=actor,
        action="create_resource",
        payload={"resource_id": str(resource.id), "title": resource.title},
    )
    await db.commit()
    await db.refresh(resource)
    return _to_admin_response(resource)


@router.patch("/{resource_id}", response_model=ResourceResponse)
async def update_admin_resource(
    resource_id: UUID,
    file: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    categories: str | None = Form(None),
    pages: int | None = Form(None),
    author: str | None = Form(None),
    accent: str | None = Form(None),
    objective: str | None = Form(None),
    age_range: str | None = Form(None),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    difficulty: str | None = Form(None),
    featured: bool | None = Form(None),
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    payload_data: dict = {}
    for key, value in {
        "title": title,
        "description": description,
        "pages": pages,
        "author": author,
        "accent": accent,
        "objective": objective,
        "age_range": age_range,
        "skill": skill,
        "related_protocol": related_protocol,
        "difficulty": difficulty,
        "featured": featured,
    }.items():
        if value is not None:
            payload_data[key] = value
    if categories is not None:
        payload_data["categories"] = parse_categories_form(categories)

    body = AdminResourceUpdateBody(**payload_data)
    service = ResourceService(db)
    try:
        resource = await service.update_global_admin(
            resource_id, body, file=file if file and file.filename else None
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recurso não encontrado",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="update_resource",
        payload={"resource_id": str(resource.id), "title": resource.title},
    )
    await db.commit()
    await db.refresh(resource)
    return _to_admin_response(resource)


@router.delete("/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_resource(
    resource_id: UUID,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = ResourceService(db)
    try:
        await service.delete_global_admin(resource_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recurso não encontrado",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="delete_resource",
        payload={"resource_id": str(resource_id)},
    )
    await db.commit()
