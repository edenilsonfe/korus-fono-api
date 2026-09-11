"""Public, no-auth access to delivered reports (link + file export)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.report_delivery import PublicReportDeliveryResponse
from app.services.professional_branding import build_document_identity
from app.services.report_delivery_service import (
    InvalidReportDeliveryToken,
    load_public_delivery,
    register_delivery_download,
    register_delivery_view,
)
from app.services.report_export import REPORT_TYPE_LABELS, export_report

router = APIRouter(prefix="/report-deliveries", tags=["report-deliveries"])


@router.get("/{token}", response_model=PublicReportDeliveryResponse)
async def read_public_report_delivery(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        context = await load_public_delivery(db, token)
    except InvalidReportDeliveryToken as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    await register_delivery_view(db, context.delivery)
    return PublicReportDeliveryResponse(
        report_type=context.report.type,
        report_type_label=REPORT_TYPE_LABELS.get(context.report.type, context.report.type),
        patient_name=context.patient.name,
        professional_name=context.professional.name,
        professional_council=context.professional.council or "",
        date=context.report.date.isoformat(),
        content=context.report.content,
        expires_at=context.delivery.expires_at,
    )


@router.get("/{token}/export")
async def export_public_report_delivery(
    token: str,
    format: str = Query("pdf", pattern="^(pdf|docx|txt|md)$"),
    db: AsyncSession = Depends(get_db),
):
    try:
        context = await load_public_delivery(db, token)
    except InvalidReportDeliveryToken as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    identity = await build_document_identity(context.professional)
    try:
        data, media_type, suffix = export_report(
            format,
            context.report.type,
            context.patient.name,
            context.report.date,
            context.report.content,
            identity=identity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await register_delivery_download(db, context.delivery)
    filename = f"relatorio-{context.report.type}-{context.report.date.isoformat()}.{suffix}"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
