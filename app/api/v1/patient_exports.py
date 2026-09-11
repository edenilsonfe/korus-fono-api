"""F6 — exportação do prontuário: resumo B e dossiê selecionável.

``GET /patients/{id}/export.pdf`` é o resumo minimizado (design B do spike 017).
``POST /patients/{id}/record-exports`` é a segunda ação, claramente separada:
dossiê PDF/ZIP com seções selecionadas, período, limites duros e auditoria.
O histórico (``GET``) expõe somente metadados — nunca bytes, URLs ou conteúdo.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.db.session import get_db
from app.models.patient import Patient
from app.models.patient_record_export import PatientRecordExport
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.patient_export import (
    PatientRecordExportRequest,
    PatientRecordExportResponse,
    PatientSummaryExportQuery,
)
from app.services import patient_record_export as patient_record_export_service

router = APIRouter(prefix="/patients", tags=["patient-exports"])


def _export_response(row: PatientRecordExport) -> PatientRecordExportResponse:
    return PatientRecordExportResponse(
        id=str(row.id),
        kind=row.kind,
        format=row.format,
        sections=list(row.sections or []),
        from_date=row.from_date,
        to_date=row.to_date,
        purpose=row.purpose,
        status=row.status,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        actor_professional_id=str(row.professional_id),
        record_counts=row.record_counts,
        attachment_count=row.attachment_count,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        error_code=row.error_code,
    )


@router.get("/{patient_id}/export.pdf")
async def download_patient_summary_pdf(
    query: Annotated[PatientSummaryExportQuery, Query()],
    patient: Patient = Depends(get_patient_for_professional),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Resumo B do paciente em PDF (somente dono; 404 fora do escopo)."""
    pdf = await patient_record_export_service.generate_patient_summary_export(
        db,
        patient=patient,
        professional=professional,
        sessions_limit=query.sessions_limit,
    )
    filename = f"paciente-{patient.id}-resumo.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{patient_id}/record-exports")
async def create_patient_record_export(
    body: PatientRecordExportRequest,
    patient: Patient = Depends(get_patient_for_professional),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Dossiê selecionável em PDF ou ZIP (somente dono; limites rejeitam)."""
    export = await patient_record_export_service.generate_patient_record_export(
        db, patient=patient, professional=professional, request=body
    )
    return Response(
        content=export.payload,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "X-Export-Id": str(export.export_id),
        },
    )


@router.get(
    "/{patient_id}/record-exports",
    response_model=PaginatedResponse[PatientRecordExportResponse],
)
async def list_patient_record_exports(
    patient: Patient = Depends(get_patient_for_professional),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[PatientRecordExportResponse]:
    """Histórico de exportações do paciente, sem bytes/URLs/conteúdo."""
    rows, total = await patient_record_export_service.list_patient_record_exports(
        db, patient_id=patient.id, page=page, limit=limit
    )
    return PaginatedResponse[PatientRecordExportResponse](
        items=[_export_response(row) for row in rows],
        total=total,
        page=page,
        limit=limit,
    )
