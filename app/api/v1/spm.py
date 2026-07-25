from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.spm import (
    SpmBatteryCreate,
    SpmBatteryResponse,
    SpmBatteryScopeUpdate,
    SpmBatteryScoreRequest,
    SpmCatalogSubform,
    SpmClinicalReportUpdate,
    SpmInformantLinkCreate,
    SpmInformantLinkCreated,
    SpmInformantLinkWhatsAppSend,
    SpmInformantLinkWhatsAppSent,
    SpmBatterySummary,
    SpmSubformAnswersUpdate,
    SpmSubformFormResponse,
    SpmSubformScoreRequest,
    SpmSuggestScopeResponse,
)
from app.services.spm_battery_service import SpmBatteryService
from app.services.spm_content_package import get_spm_content_package
from app.services.spm_scoring_service import compute_subform_scores, synthesize_battery_scores

router = APIRouter(prefix="/spm", tags=["spm"])


@router.get("/catalog/subforms", response_model=list[SpmCatalogSubform])
async def list_spm_subforms(_professional: Professional = Depends(require_verified_professional)):
    package = get_spm_content_package()
    return [SpmCatalogSubform(**entry) for entry in package.list_subforms()]


@router.get(
    "/catalog/subforms/{subform_slug}/preview",
    response_model=SpmSubformFormResponse,
)
async def preview_spm_subform(
    subform_slug: str,
    _professional: Professional = Depends(require_verified_professional),
):
    package = get_spm_content_package()
    try:
        config = package.get_subform(subform_slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Sub-forma não encontrada") from exc
    items = package.public_items_payload(subform_slug)
    if not items:
        raise HTTPException(status_code=404, detail="Sub-forma sem itens disponíveis")
    return SpmSubformFormResponse(
        subform_slug=subform_slug,
        title=config["title"],
        scale=package.scale,
        domains=package.domains,
        items=items,
        filler=config["filler"],
    )


@router.get("/subforms/{subform_slug}/form", response_model=SpmSubformFormResponse)
async def get_spm_subform_form(
    subform_slug: str,
    _professional: Professional = Depends(require_verified_professional),
):
    package = get_spm_content_package()
    try:
        config = package.get_subform(subform_slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Sub-forma não encontrada") from exc
    if config["filler"] != "clinical":
        raise HTTPException(status_code=400, detail="Sub-forma preenchida via link externo")
    return SpmSubformFormResponse(
        subform_slug=subform_slug,
        title=config["title"],
        scale=package.scale,
        domains=package.domains,
        items=package.public_items_payload(subform_slug),
        filler=config["filler"],
    )


@router.get("/suggest-scope", response_model=SpmSuggestScopeResponse)
async def suggest_spm_scope(
    patient_id: UUID = Query(...),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    service = SpmBatteryService(db)
    suggested, age = await service.suggest_scope_for_patient(patient_id, professional.id)
    return SpmSuggestScopeResponse(suggested=suggested, age_months=age)


@router.get("/batteries", response_model=PaginatedResponse[SpmBatterySummary])
async def list_spm_batteries(
    patient_id: UUID | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if patient_id:
        await get_patient_for_professional(patient_id, professional, db)
    service = SpmBatteryService(db)
    items, total = await service.list_batteries(
        professional_id=professional.id,
        patient_id=patient_id,
        status_filter=status,
        page=page,
        limit=limit,
    )
    return PaginatedResponse(items=items, total=total, page=page, limit=limit)


@router.post("/batteries", response_model=SpmBatteryResponse, status_code=status.HTTP_201_CREATED)
async def create_spm_battery(
    data: SpmBatteryCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(data.patient_id, professional, db)
    service = SpmBatteryService(db)
    return await service.create_battery(data=data, professional_id=professional.id)


@router.get("/batteries/{battery_id}", response_model=SpmBatteryResponse)
async def get_spm_battery(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.get_battery(battery_id, professional_id=professional.id)


@router.patch("/batteries/{battery_id}/scope", response_model=SpmBatteryResponse)
async def update_spm_battery_scope(
    battery_id: UUID,
    data: SpmBatteryScopeUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.update_scope(
        battery_id, data, professional_id=professional.id
    )


@router.patch("/batteries/{battery_id}/clinical-report", response_model=SpmBatteryResponse)
async def update_spm_clinical_report(
    battery_id: UUID,
    data: SpmClinicalReportUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.update_clinical_report(
        battery_id, data, professional_id=professional.id
    )


@router.patch(
    "/batteries/{battery_id}/subforms/{subform_slug}",
    response_model=SpmBatteryResponse,
)
async def update_spm_clinical_subform(
    battery_id: UUID,
    subform_slug: str,
    data: SpmSubformAnswersUpdate,
    finalize: bool = Query(False),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.update_clinical_subform(
        battery_id,
        subform_slug,
        data,
        professional_id=professional.id,
        finalize=finalize,
    )


@router.post(
    "/batteries/{battery_id}/subforms/{subform_slug}/links",
    response_model=SpmInformantLinkCreated,
)
async def create_spm_informant_link(
    battery_id: UUID,
    subform_slug: str,
    data: SpmInformantLinkCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.create_informant_link(
        battery_id,
        subform_slug,
        data,
        professional_id=professional.id,
    )


@router.post(
    "/batteries/{battery_id}/subforms/{subform_slug}/links/send-whatsapp",
    response_model=SpmInformantLinkWhatsAppSent,
)
async def send_spm_informant_link_whatsapp(
    battery_id: UUID,
    subform_slug: str,
    data: SpmInformantLinkWhatsAppSend,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.send_informant_link_whatsapp(
        battery_id,
        subform_slug,
        data,
        professional_id=professional.id,
    )


@router.delete(
    "/batteries/{battery_id}/subforms/{subform_slug}/links/active",
    response_model=SpmBatteryResponse,
)
async def revoke_spm_informant_link(
    battery_id: UUID,
    subform_slug: str,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.revoke_active_link(
        battery_id, subform_slug, professional_id=professional.id
    )


@router.post("/batteries/{battery_id}/finalize", response_model=SpmBatteryResponse)
async def finalize_spm_battery(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.finalize_battery(battery_id, professional_id=professional.id)


@router.post("/batteries/{battery_id}/cancel", response_model=SpmBatteryResponse)
async def cancel_spm_battery(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = SpmBatteryService(db)
    return await service.cancel_battery(battery_id, professional_id=professional.id)


@router.post("/subforms/{subform_slug}/score")
async def score_spm_subform(
    subform_slug: str,
    body: SpmSubformScoreRequest,
    _professional: Professional = Depends(require_verified_professional),
):
    package = get_spm_content_package()
    try:
        scores = compute_subform_scores(package, subform_slug, body.answers)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Sub-forma não encontrada") from exc
    return scores


@router.post("/score-battery")
async def score_spm_battery(
    body: SpmBatteryScoreRequest,
    _professional: Professional = Depends(require_verified_professional),
):
    package = get_spm_content_package()
    subform_scores = []
    for entry in body.subforms:
        slug = entry.get("slug")
        answers = entry.get("answers") or {}
        if not slug:
            continue
        try:
            subform_scores.append(compute_subform_scores(package, slug, answers))
        except KeyError:
            continue
    if not subform_scores:
        raise HTTPException(status_code=400, detail="Nenhuma sub-forma válida para pontuar")
    return synthesize_battery_scores(subform_scores)
