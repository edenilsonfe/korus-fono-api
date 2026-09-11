"""F3 routes: selectable report sources and consolidated composition read-back.

Kept separate from ``app/api/v1/ai.py`` so the legacy report routes stay
untouched; both routers are registered in ``app/api/v1/router.py``.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.db.session import get_db
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.common import PaginatedResponse
from app.schemas.report_composition import (
    ReportCompositionResponse,
    ReportSourceResponse,
)
from app.services import report_composition_service

router = APIRouter(prefix="/patients", tags=["report-compositions"])
ai_reports_router = APIRouter(prefix="/ai", tags=["report-compositions"])


@router.get(
    "/{patient_id}/report-sources",
    response_model=PaginatedResponse[ReportSourceResponse],
)
async def list_patient_report_sources(
    kind: str = Query(..., description="assessment | evolution | session | goal"),
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    patient: Patient = Depends(get_patient_for_professional),
    db: AsyncSession = Depends(get_db),
):
    """Fontes selecionáveis do paciente (somente dono; avaliações concluídas)."""
    items, total = await report_composition_service.list_report_sources(
        db,
        patient_id=patient.id,
        kind=kind,
        from_date=from_date,
        to_date=to_date,
        page=page,
        limit=limit,
    )
    return PaginatedResponse[ReportSourceResponse](
        items=[ReportSourceResponse(**item) for item in items],
        total=total,
        page=page,
        limit=limit,
    )


@ai_reports_router.get(
    "/reports/{report_id}/composition",
    response_model=ReportCompositionResponse,
)
async def get_report_composition(
    report_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Proveniência restrita da seleção de um laudo consolidado."""
    composition = await report_composition_service.get_report_composition(
        db, report_id=report_id, professional=professional
    )
    snapshot = composition.snapshot or {}
    return ReportCompositionResponse(
        id=str(composition.id),
        report_id=str(composition.report_id),
        template_version=composition.template_version,
        captured_at=composition.captured_at,
        context_hash=composition.context_hash,
        supersedes_report_id=(
            str(composition.supersedes_report_id) if composition.supersedes_report_id else None
        ),
        sources=snapshot.get("sources") or [],
        comparisons=snapshot.get("comparisons") or [],
        warnings=snapshot.get("warnings") or [],
    )
