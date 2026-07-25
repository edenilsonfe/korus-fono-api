"""Professional-facing resources library endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.resource import (
    ResourceCreateBody,
    ResourceDownloadUrl,
    ResourceResponse,
    ResourceScope,
    ResourceUpdateBody,
)
from app.services.resource_service import (
    ResourceForbiddenError,
    ResourceNotFoundError,
    ResourceService,
    parse_categories_form,
    to_resource_response,
)

router = APIRouter(prefix="/resources", tags=["resources"])


def _http_not_found(exc: ResourceNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurso não encontrado")


def _http_forbidden(exc: ResourceForbiddenError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado ao recurso")


@router.get("", response_model=list[ResourceResponse])
@router.get("/", response_model=list[ResourceResponse])
async def list_resources(
    q: str | None = Query(None),
    category: str | None = Query(None),
    scope: ResourceScope = Query("all"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = ResourceService(db)
    items = await service.list_for_professional(
        professional, q=q, category=category, scope=scope
    )
    return [to_resource_response(item, professional.id) for item in items]


@router.get("/{resource_id}/download-url", response_model=ResourceDownloadUrl)
async def get_resource_download_url(
    resource_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = ResourceService(db)
    try:
        url = await service.download_url(professional, resource_id)
    except ResourceNotFoundError as exc:
        raise _http_not_found(exc) from exc
    except ResourceForbiddenError as exc:
        raise _http_forbidden(exc) from exc
    await db.commit()
    return ResourceDownloadUrl(url=url)


@router.post("", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
async def create_personal_resource(
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
    shared_with_platform: bool = Form(False),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    body = ResourceCreateBody(
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
        shared_with_platform=shared_with_platform,
    )
    service = ResourceService(db)
    resource = await service.create_personal(professional, file=file, body=body)
    await db.commit()
    await db.refresh(resource)
    return to_resource_response(resource, professional.id)


@router.patch("/{resource_id}", response_model=ResourceResponse)
async def update_personal_resource(
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
    shared_with_platform: bool | None = Form(None),
    professional: Professional = Depends(require_verified_professional),
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
        "shared_with_platform": shared_with_platform,
    }.items():
        if value is not None:
            payload_data[key] = value
    if categories is not None:
        payload_data["categories"] = parse_categories_form(categories)

    body = ResourceUpdateBody(**payload_data)
    service = ResourceService(db)
    try:
        resource = await service.update_personal(
            professional, resource_id, body, file=file if file and file.filename else None
        )
    except ResourceNotFoundError as exc:
        raise _http_not_found(exc) from exc
    except ResourceForbiddenError as exc:
        raise _http_forbidden(exc) from exc
    await db.commit()
    await db.refresh(resource)
    return to_resource_response(resource, professional.id)


@router.delete("/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_resource(
    resource_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = ResourceService(db)
    try:
        await service.delete_personal(professional, resource_id)
    except ResourceNotFoundError as exc:
        raise _http_not_found(exc) from exc
    except ResourceForbiddenError as exc:
        raise _http_forbidden(exc) from exc
    await db.commit()
