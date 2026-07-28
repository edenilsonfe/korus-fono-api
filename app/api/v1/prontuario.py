from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mappers import format_size_bytes
from app.core.config import get_settings
from app.core.deps import get_patient_for_professional, require_verified_professional
from app.core.utils import utcnow
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.evolution import Evolution
from app.models.professional import Professional
from app.schemas.prontuario import (
    AnamneseBulkUpsert,
    AnamneseComplete,
    AnamneseCreate,
    AnamneseDocumentResponse,
    AnamneseEntryInput,
    AnamneseResponse,
    EvolutionCreate,
    EvolutionResponse,
)
from app.services import anamnese_service
from app.services.attachment_upload import (
    validate_attachment_category,
    validate_attachment_upload,
)
from app.services.storage import storage_service
from app.services.clinical_activity import record_evolution
from app.services.timeline import create_timeline_event

router = APIRouter(prefix="/patients/{patient_id}", tags=["prontuario"])


@router.get("/evolutions", response_model=list[EvolutionResponse])
async def list_evolutions(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(
        select(Evolution).where(Evolution.patient_id == patient_id).order_by(Evolution.date.desc())
    )
    return [
        EvolutionResponse(
            id=str(e.id),
            patient_id=str(e.patient_id),
            session_id=str(e.session_id) if e.session_id else None,
            date=e.date.isoformat(),
            title=e.title,
            content=e.content,
            professional=professional.name,
        )
        for e in result.scalars().all()
    ]


@router.post("/evolutions", response_model=EvolutionResponse, status_code=status.HTTP_201_CREATED)
async def create_evolution(
    patient_id: UUID,
    body: EvolutionCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    evolution = Evolution(
        patient_id=patient_id,
        session_id=UUID(body.session_id) if body.session_id else None,
        professional_id=professional.id,
        date=utcnow(),
        title=body.title,
        content=body.content,
    )
    db.add(evolution)
    await db.flush()
    await record_evolution(db, evolution=evolution, professional=professional)
    return EvolutionResponse(
        id=str(evolution.id),
        patient_id=str(patient_id),
        session_id=body.session_id,
        date=evolution.date.isoformat(),
        title=evolution.title,
        content=evolution.content,
        professional=professional.name,
    )


@router.get("/anamnese", response_model=AnamneseDocumentResponse)
async def list_anamnese(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    entries = await anamnese_service.list_entries(db, patient_id)
    return anamnese_service.document_response(patient, entries)


@router.put("/anamnese", response_model=AnamneseDocumentResponse)
async def bulk_upsert_anamnese(
    patient_id: UUID,
    body: AnamneseBulkUpsert,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    anamnese_service.assert_editable(patient)
    entries = await anamnese_service.upsert_entries(db, patient_id=patient_id, entries=body.entries)
    return anamnese_service.document_response(patient, entries)


@router.post("/anamnese/complete", response_model=AnamneseDocumentResponse)
async def complete_anamnese(
    patient_id: UUID,
    body: AnamneseComplete,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    return await anamnese_service.complete_anamnese(db, patient=patient, entries=body.entries)


@router.post("/anamnese", response_model=AnamneseResponse, status_code=status.HTTP_201_CREATED)
async def upsert_anamnese(
    patient_id: UUID,
    body: AnamneseCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    anamnese_service.assert_editable(patient)
    entries = await anamnese_service.upsert_entries(
        db,
        patient_id=patient_id,
        entries=[AnamneseEntryInput(section=body.section, value=body.value)],
    )
    section = body.section.strip()
    entry = next(e for e in entries if e.section == section)
    return AnamneseResponse(
        id=str(entry.id),
        patient_id=str(patient_id),
        section=entry.section,
        value=entry.value,
    )


UPLOAD_READ_CHUNK_SIZE = 1024 * 1024

CATEGORY_TIMELINE_MAP = {
    "video": "video",
    "audio": "audio",
    "foto": "foto",
    "relatorio": "documento",
}


@router.get("/attachments")
async def list_attachments(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(
        select(Attachment).where(Attachment.patient_id == patient_id).order_by(Attachment.date.desc())
    )
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "category": a.category,
            "date": a.date.isoformat(),
            "sizeBytes": a.size_bytes,
            "size": format_size_bytes(a.size_bytes),
        }
        for a in result.scalars().all()
    ]


@router.post("/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    patient_id: UUID,
    file: UploadFile = File(...),
    category: str = "relatorio",
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    category = validate_attachment_category(category)
    max_bytes = get_settings().max_upload_bytes
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
                    f"Arquivo excede o tamanho máximo permitido de {format_size_bytes(max_bytes)}."
                ),
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type, safe_name = validate_attachment_upload(
        content_type=file.content_type,
        filename=file.filename,
        body=body,
    )
    key = storage_service.make_key(patient_id, safe_name)
    await storage_service.upload(key, body, content_type)
    attachment = Attachment(
        patient_id=patient_id,
        professional_id=professional.id,
        name=safe_name,
        category=category,
        size_bytes=len(body),
        storage_key=key,
        date=utcnow(),
    )
    db.add(attachment)
    await db.flush()
    event_type = CATEGORY_TIMELINE_MAP.get(category, "documento")
    await create_timeline_event(
        db,
        patient_id=patient_id,
        professional_id=professional.id,
        event_type=event_type,
        title=f"Arquivo adicionado: {attachment.name}",
        description=category,
        source_id=attachment.id,
    )
    return {
        "id": str(attachment.id),
        "name": attachment.name,
        "category": attachment.category,
        "date": attachment.date.isoformat(),
        "sizeBytes": attachment.size_bytes,
        "size": format_size_bytes(attachment.size_bytes),
    }


@router.get("/attachments/{attachment_id}/url")
async def get_attachment_url(
    patient_id: UUID,
    attachment_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(
        select(Attachment).where(Attachment.id == attachment_id, Attachment.patient_id == patient_id)
    )
    attachment = result.scalar_one_or_none()
    if not attachment:
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    # Preview (img/iframe) needs inline; downloads still get a safe filename.
    url = await storage_service.presigned_url(
        attachment.storage_key,
        filename=attachment.name,
        as_attachment=False,
    )
    return {"url": url}
