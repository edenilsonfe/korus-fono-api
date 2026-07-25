from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.battery import (
    BatteryCreate,
    BatteryEventCreate,
    BatteryEventUpdate,
    BatteryEvidenceCreate,
    BatteryFinalizeRequest,
    BatteryResponse,
    BatterySubformAnswersUpdate,
    BatterySubformFormResponse,
)
from app.schemas.common import PaginatedResponse
from app.services.battery_evidence_service import BatteryEvidenceService
from app.services.battery_report_service import export_battery_pdf
from app.services.battery_service import BatteryService
from app.services.instrument_content_package import get_instrument_content_package

router = APIRouter(prefix="/batteries", tags=["batteries"])


@router.post("", response_model=BatteryResponse, status_code=status.HTTP_201_CREATED)
async def create_battery(
    data: BatteryCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    return await service.create_battery(data=data, professional_id=professional.id)


@router.get("", response_model=PaginatedResponse)
async def list_batteries(
    instrument_slug: str | None = Query(None, alias="instrumentSlug"),
    patient_id: UUID | None = Query(None, alias="patientId"),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    items, total = await service.list_batteries(
        professional_id=professional.id,
        instrument_slug=instrument_slug,
        patient_id=patient_id,
        status_filter=status_filter,
        page=page,
        limit=limit,
    )
    return PaginatedResponse(items=items, total=total, page=page, limit=limit)


@router.get("/{battery_id}", response_model=BatteryResponse)
async def get_battery(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    return await service.get_battery(battery_id, professional_id=professional.id)


@router.get("/{battery_id}/subforms/{subform_slug}/form", response_model=BatterySubformFormResponse)
async def get_battery_subform_form(
    battery_id: UUID,
    subform_slug: str,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    return await service.get_subform_form(
        battery_id, subform_slug, professional_id=professional.id
    )


@router.patch("/{battery_id}/subforms/{subform_slug}", response_model=BatteryResponse)
async def update_battery_subform(
    battery_id: UUID,
    subform_slug: str,
    data: BatterySubformAnswersUpdate,
    finalize: bool = Query(False),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    return await service.update_subform(
        battery_id,
        subform_slug,
        data,
        professional_id=professional.id,
        finalize=finalize,
    )


@router.post("/{battery_id}/finalize", response_model=BatteryResponse)
async def finalize_battery(
    battery_id: UUID,
    body: BatteryFinalizeRequest | None = None,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    conclusion = body.clinical_conclusion if body else ""
    return await service.finalize_battery(
        battery_id,
        professional_id=professional.id,
        clinical_conclusion=conclusion,
    )


@router.post("/{battery_id}/cancel", response_model=BatteryResponse)
async def cancel_battery(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    return await service.cancel_battery(battery_id, professional_id=professional.id)


@router.get("/{battery_id}/report")
async def download_battery_report(
    battery_id: UUID,
    format: str = Query("pdf", pattern="^(pdf)$"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryService(db)
    battery = await service.get_battery(battery_id, professional_id=professional.id)
    if battery.status != "completed":
        raise HTTPException(status_code=400, detail="Relatório disponível apenas para baterias finalizadas")
    try:
        package = get_instrument_content_package(battery.instrument_slug)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Pacote não encontrado") from exc
    pdf_bytes = export_battery_pdf(battery, package)
    slug = battery.instrument_slug or "battery"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{slug}-{battery_id}.pdf"'},
    )


@router.get("/{battery_id}/evidences")
async def list_battery_evidences(
    battery_id: UUID,
    subform: str | None = Query(None),
    item_id: str | None = Query(None, alias="itemId"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    return await service.list_evidences(
        battery_id,
        professional_id=professional.id,
        subform_slug=subform,
        item_id=item_id,
    )


@router.post("/{battery_id}/evidences")
async def create_battery_evidence_note(
    battery_id: UUID,
    data: BatteryEvidenceCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    return await service.create_note_evidence(
        battery_id,
        professional_id=professional.id,
        kind="note",
        note_text=data.note_text,
        subform_slug=data.subform_slug,
        item_id=data.item_id,
        recorded_at=data.recorded_at,
    )


@router.post("/{battery_id}/evidences/upload")
async def upload_battery_evidence(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    kind: str = Form(...),
    subform_slug: str | None = Form(None),
    item_id: str | None = Form(None),
):
    service = BatteryEvidenceService(db)
    return await service.upload_evidence(
        battery_id,
        professional_id=professional.id,
        file=file,
        kind=kind,
        subform_slug=subform_slug,
        item_id=item_id,
    )


@router.delete("/{battery_id}/evidences/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_battery_evidence(
    battery_id: UUID,
    evidence_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    await service.delete_evidence(battery_id, evidence_id, professional_id=professional.id)


@router.get("/{battery_id}/evidences/{evidence_id}/url")
async def get_battery_evidence_url(
    battery_id: UUID,
    evidence_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    url = await service.get_evidence_url(battery_id, evidence_id, professional_id=professional.id)
    return {"url": url}


@router.get("/{battery_id}/events")
async def list_battery_events(
    battery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    return await service.list_events(battery_id, professional_id=professional.id)


@router.post("/{battery_id}/events")
async def create_battery_event(
    battery_id: UUID,
    data: BatteryEventCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    return await service.create_event(
        battery_id,
        professional_id=professional.id,
        text=data.text,
        occurred_at=data.occurred_at,
        subform_slug=data.subform_slug,
        item_id=data.item_id,
        evidence_id=data.evidence_id,
    )


@router.patch("/{battery_id}/events/{event_id}")
async def update_battery_event(
    battery_id: UUID,
    event_id: UUID,
    data: BatteryEventUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    return await service.update_event(
        battery_id,
        event_id,
        professional_id=professional.id,
        text=data.text,
        occurred_at=data.occurred_at,
        subform_slug=data.subform_slug,
        item_id=data.item_id,
    )


@router.delete("/{battery_id}/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_battery_event(
    battery_id: UUID,
    event_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = BatteryEvidenceService(db)
    await service.delete_event(battery_id, event_id, professional_id=professional.id)
