"""Professional-facing resources library endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import CLINICAL_DOMAIN_CATALOG
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
from app.schemas.resource_license import ResourceLicenseDeclaration, ResourceLicenseResponse
from app.schemas.resource_link import ResourceDomainsUpdate
from app.services.resource_license_service import (
    LICENSE_ERRORS,
    ResourceLicenseService,
    decisions_for,
    get_current_license,
    license_http_error,
    to_license_response,
)
from app.services.resource_link_service import (
    LINK_ERRORS,
    domain_keys_for_resource,
    link_http_error,
    replace_domain_links,
)
from app.services.resource_service import (
    ResourceForbiddenError,
    ResourceHasReferencesError,
    ResourceNotFoundError,
    ResourceService,
    apply_clear_fields,
    parse_categories_form,
    parse_clear_fields_form,
    resolve_compat_field,
    resolve_compat_flag,
    to_resource_response,
)
from app.services.storage import safe_content_disposition_filename

router = APIRouter(prefix="/resources", tags=["resources"])

_VALID_DOMAIN_KEYS = {domain["key"] for domain in CLINICAL_DOMAIN_CATALOG}


def _http_not_found(exc: ResourceNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurso não encontrado")


def _http_forbidden(exc: ResourceForbiddenError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado ao recurso")


def _http_referenced(exc: ResourceHasReferencesError) -> HTTPException:
    detail = getattr(exc, "detail", None) or "Cadastre uma nova versão do material."
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    )


@router.get("", response_model=list[ResourceResponse])
@router.get("/", response_model=list[ResourceResponse])
async def list_resources(
    q: str | None = Query(None),
    category: str | None = Query(None),
    scope: ResourceScope = Query("all"),
    domain_key: str | None = Query(None, alias="domainKey"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if domain_key is not None and domain_key not in _VALID_DOMAIN_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Domínio clínico inválido.",
        )
    service = ResourceService(db)
    items = await service.list_for_professional(
        professional,
        q=q,
        category=category,
        scope=scope,
        domain_key=domain_key,
        offset=offset,
        limit=limit,
    )
    return [
        to_resource_response(resource, professional.id, license=license, domain_keys=keys)
        for resource, license, keys in items
    ]


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


@router.get("/{resource_id}/file")
async def get_resource_file(
    resource_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Stream the object same-origin (inline) — CSP/mixed-content-safe preview."""
    service = ResourceService(db)
    try:
        body, resource = await service.download_content(professional, resource_id)
    except ResourceNotFoundError as exc:
        raise _http_not_found(exc) from exc
    except ResourceForbiddenError as exc:
        raise _http_forbidden(exc) from exc
    await db.commit()
    safe = safe_content_disposition_filename(resource.storage_key, resource.title)
    return Response(
        content=body,
        media_type=resource.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe}"'},
    )


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
    age_range_camel: str | None = Form(None, alias="ageRange"),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    related_protocol_camel: str | None = Form(None, alias="relatedProtocol"),
    difficulty: str | None = Form(None),
    shared_with_platform: bool | None = Form(None),
    shared_with_platform_camel: bool | None = Form(None, alias="sharedWithPlatform"),
    clear_fields: str | None = Form(None, alias="clearFields"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    age_range_value = resolve_compat_field(
        value=age_range_camel, legacy=age_range, name="ageRange", legacy_name="age_range"
    )
    related_protocol_value = resolve_compat_field(
        value=related_protocol_camel,
        legacy=related_protocol,
        name="relatedProtocol",
        legacy_name="related_protocol",
    )
    shared_value = resolve_compat_flag(
        value=shared_with_platform_camel,
        legacy=shared_with_platform,
        name="sharedWithPlatform",
        legacy_name="shared_with_platform",
    )
    payload_data: dict = {}
    for key, value in {
        "title": title,
        "description": description,
        "pages": pages,
        "author": author,
        "accent": accent,
        "objective": objective,
        "age_range": age_range_value,
        "skill": skill,
        "related_protocol": related_protocol_value,
        "difficulty": difficulty,
    }.items():
        if value is not None:
            payload_data[key] = value
    payload_data["categories"] = parse_categories_form(categories)
    payload_data["shared_with_platform"] = shared_value if shared_value is not None else False
    apply_clear_fields(
        payload_data,
        parse_clear_fields_form(clear_fields),
        provided={
            "objective": objective,
            "ageRange": age_range_value,
            "skill": skill,
            "relatedProtocol": related_protocol_value,
            "difficulty": difficulty,
            "pages": pages,
        },
    )
    body = ResourceCreateBody(**payload_data)
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
    age_range_camel: str | None = Form(None, alias="ageRange"),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    related_protocol_camel: str | None = Form(None, alias="relatedProtocol"),
    difficulty: str | None = Form(None),
    shared_with_platform: bool | None = Form(None),
    shared_with_platform_camel: bool | None = Form(None, alias="sharedWithPlatform"),
    clear_fields: str | None = Form(None, alias="clearFields"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    age_range_value = resolve_compat_field(
        value=age_range_camel, legacy=age_range, name="ageRange", legacy_name="age_range"
    )
    related_protocol_value = resolve_compat_field(
        value=related_protocol_camel,
        legacy=related_protocol,
        name="relatedProtocol",
        legacy_name="related_protocol",
    )
    shared_value = resolve_compat_flag(
        value=shared_with_platform_camel,
        legacy=shared_with_platform,
        name="sharedWithPlatform",
        legacy_name="shared_with_platform",
    )
    payload_data: dict = {}
    for key, value in {
        "title": title,
        "description": description,
        "pages": pages,
        "author": author,
        "accent": accent,
        "objective": objective,
        "age_range": age_range_value,
        "skill": skill,
        "related_protocol": related_protocol_value,
        "difficulty": difficulty,
        "shared_with_platform": shared_value,
    }.items():
        if value is not None:
            payload_data[key] = value
    if categories is not None:
        payload_data["categories"] = parse_categories_form(categories)
    apply_clear_fields(
        payload_data,
        parse_clear_fields_form(clear_fields),
        provided={
            "objective": objective,
            "ageRange": age_range_value,
            "skill": skill,
            "relatedProtocol": related_protocol_value,
            "difficulty": difficulty,
            "pages": pages,
        },
    )

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
    except ResourceHasReferencesError as exc:
        raise _http_referenced(exc) from exc
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    domain_keys = await domain_keys_for_resource(db, resource.id)
    return to_resource_response(
        resource, professional.id, license=license, domain_keys=domain_keys
    )


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
    except ResourceHasReferencesError as exc:
        # Recurso referenciado (vínculo/licença/entrega) → 409; a UI oferece
        # arquivar, porque excluir apagaria histórico.
        raise _http_referenced(exc) from exc
    await db.commit()


@router.post("/{resource_id}/archive", response_model=ResourceResponse)
async def archive_personal_resource(
    resource_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Arquiva o próprio material (idempotente), sem destruir o histórico.

    Bloqueia novas distribuições/prescrições e revoga a disponibilização
    externa; arquivo, licenças e vínculos permanecem. Material arquivado não é
    republicado — novo conteúdo exige um novo Resource.
    """
    service = ResourceService(db)
    try:
        resource = await service.archive_personal(professional, resource_id)
    except ResourceNotFoundError as exc:
        raise _http_not_found(exc) from exc
    except ResourceForbiddenError as exc:
        raise _http_forbidden(exc) from exc
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    domain_keys = await domain_keys_for_resource(db, resource.id)
    return to_resource_response(
        resource, professional.id, license=license, domain_keys=domain_keys
    )


@router.get("/{resource_id}/license", response_model=ResourceLicenseResponse | None)
async def get_personal_resource_license(
    resource_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Licença vigente do próprio material (null quando ainda não declarada)."""
    service = ResourceLicenseService(db)
    try:
        _resource, license = await service.get_for_owner(professional, resource_id)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    if license is None:
        return None
    return to_license_response(license, await decisions_for(db, license.id))


@router.put("/{resource_id}/license", response_model=ResourceLicenseResponse)
async def declare_personal_resource_license(
    resource_id: UUID,
    body: ResourceLicenseDeclaration,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Nova declaração do dono substitui a concessão atual preservando o histórico."""
    service = ResourceLicenseService(db)
    try:
        license = await service.declare_personal(professional, resource_id, body)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    await db.commit()
    await db.refresh(license)
    return to_license_response(license)


@router.put("/{resource_id}/domains", response_model=ResourceResponse)
async def update_personal_resource_domains(
    resource_id: UUID,
    body: ResourceDomainsUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Substitui os domínios clínicos do próprio material (pessoal só dono)."""
    service = ResourceService(db)
    resource = await service.get_by_id(resource_id)
    if resource is None or resource.owner_professional_id != professional.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurso não encontrado")
    try:
        keys = await replace_domain_links(db, resource, body.domain_keys, professional)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    return to_resource_response(
        resource, professional.id, license=license, domain_keys=keys
    )
