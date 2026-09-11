"""Admin endpoints for the global resources catalog (platform staff only)."""

from typing import Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
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
from app.schemas.resource_license import (
    ResourceLicenseDecisionCreate,
    ResourceLicenseDeclaration,
    ResourceLicenseResponse,
    ResourceLicenseStatus,
    ResourcePublicationUpdate,
)
from app.schemas.resource_link import ResourceDomainsUpdate
from app.services.admin_audit_service import AdminAuditService
from app.services.resource_license_service import (
    LICENSE_ERRORS,
    ResourceLicenseService,
    decisions_for,
    get_current_license,
    license_http_error,
    to_license_response,
)
from app.services.resource_link_service import (
    DOMAIN_KEYS,
    LINK_ERRORS,
    domain_keys_for_resource,
    link_http_error,
    replace_domain_links,
)
from app.services.resource_service import (
    ResourceHasReferencesError,
    ResourceNotFoundError,
    ResourceService,
    apply_clear_fields,
    parse_categories_form,
    parse_clear_fields_form,
    resolve_compat_field,
    to_resource_response,
)

router = APIRouter(prefix="/admin/resources", tags=["admin-resources"])


def _to_admin_response(resource, license=None, domain_keys=None) -> ResourceResponse:
    return to_resource_response(resource, None, license=license, domain_keys=domain_keys)


@router.get("", response_model=list[ResourceResponse])
@router.get("/", response_model=list[ResourceResponse])
async def list_admin_resources(
    publication_status: Literal["draft", "published", "archived"] | None = Query(
        None, alias="publicationStatus"
    ),
    license_status: ResourceLicenseStatus | None = Query(None, alias="licenseStatus"),
    include_submissions: bool = Query(False, alias="includeSubmissions"),
    domain_key: str | None = Query(None, alias="domainKey"),
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    if domain_key is not None and domain_key not in DOMAIN_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Domínio clínico inválido.",
        )
    service = ResourceService(db)
    items = await service.list_for_admin(
        publication_status=publication_status,
        license_status=license_status,
        include_submissions=include_submissions,
        domain_key=domain_key,
    )
    return [_to_admin_response(resource, license, keys) for resource, license, keys in items]


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
    age_range_camel: str | None = Form(None, alias="ageRange"),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    related_protocol_camel: str | None = Form(None, alias="relatedProtocol"),
    difficulty: str | None = Form(None),
    clear_fields: str | None = Form(None, alias="clearFields"),
    featured: bool = Form(False),
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
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
    payload_data["featured"] = featured
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
    body = AdminResourceCreateBody(**payload_data)
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
    age_range_camel: str | None = Form(None, alias="ageRange"),
    skill: str | None = Form(None),
    related_protocol: str | None = Form(None),
    related_protocol_camel: str | None = Form(None, alias="relatedProtocol"),
    difficulty: str | None = Form(None),
    clear_fields: str | None = Form(None, alias="clearFields"),
    featured: bool | None = Form(None),
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
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
        "featured": featured,
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
    except ResourceHasReferencesError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cadastre uma nova versão do material.",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="update_resource",
        payload={"resource_id": str(resource.id), "title": resource.title},
    )
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    domain_keys = await domain_keys_for_resource(db, resource.id)
    return _to_admin_response(resource, license, domain_keys)


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
    except ResourceHasReferencesError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=getattr(exc, "detail", None)
            or "Recurso referenciado não pode ser excluído; arquive o material.",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="delete_resource",
        payload={"resource_id": str(resource_id)},
    )
    await db.commit()


@router.get("/{resource_id}/license", response_model=ResourceLicenseResponse | None)
async def get_admin_resource_license(
    resource_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Licença vigente (com comprovação e decisões) — visão da curadoria."""
    service = ResourceLicenseService(db)
    try:
        _resource, license = await service.get_for_admin(resource_id)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    if license is None:
        return None
    return to_license_response(license, await decisions_for(db, license.id))


@router.post(
    "/{resource_id}/license/declarations",
    response_model=ResourceLicenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def declare_admin_resource_license(
    resource_id: UUID,
    body: ResourceLicenseDeclaration,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Declaração de material global em nome do titular comprovado — começa ``pending``.

    Não altera a propriedade do material nem aprova/publica automaticamente.
    """
    service = ResourceLicenseService(db)
    try:
        license = await service.declare_global_admin(actor, resource_id, body)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="declare_resource_license",
        payload={
            "resource_id": str(resource_id),
            "license_id": str(license.id),
            "version": license.version,
            "rights_holder": license.rights_holder,
        },
    )
    await db.commit()
    await db.refresh(license)
    return to_license_response(license)


@router.put("/{resource_id}/license", response_model=ResourceLicenseResponse)
async def decide_admin_resource_license(
    resource_id: UUID,
    body: ResourceLicenseDecisionCreate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Aprova/rejeita/revoga a licença vigente — decisão auditada, sem mudar o dono."""
    service = ResourceLicenseService(db)
    try:
        _resource, license, _decision = await service.decide_admin(actor, resource_id, body)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="decide_resource_license",
        payload={
            "resource_id": str(resource_id),
            "license_id": str(license.id),
            "decision": body.decision,
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(license)
    return to_license_response(license, await decisions_for(db, license.id))


@router.patch("/{resource_id}/publication", response_model=ResourceResponse)
async def update_admin_resource_publication(
    resource_id: UUID,
    body: ResourcePublicationUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Publica (exige licença aprovada/válida + arquivo disponível) ou arquiva."""
    service = ResourceLicenseService(db)
    try:
        resource = await service.set_publication(resource_id, body)
    except LICENSE_ERRORS as exc:
        raise license_http_error(exc) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="publish_resource" if body.status == "published" else "archive_resource",
        payload={
            "resource_id": str(resource_id),
            "title": resource.title,
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    domain_keys = await domain_keys_for_resource(db, resource.id)
    return _to_admin_response(resource, license, domain_keys)


@router.put("/{resource_id}/domains", response_model=ResourceResponse)
async def update_admin_resource_domains(
    resource_id: UUID,
    body: ResourceDomainsUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Substitui os domínios clínicos de material global (product:write)."""
    service = ResourceService(db)
    resource = await service.get_by_id(resource_id)
    if resource is None or resource.owner_professional_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recurso não encontrado",
        )
    try:
        keys = await replace_domain_links(db, resource, body.domain_keys, actor)
    except LINK_ERRORS as exc:
        raise link_http_error(exc) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="update_resource_domains",
        payload={"resource_id": str(resource_id), "domainKeys": keys},
    )
    await db.commit()
    await db.refresh(resource)
    license = await get_current_license(db, resource.id)
    return _to_admin_response(resource, license, keys)
