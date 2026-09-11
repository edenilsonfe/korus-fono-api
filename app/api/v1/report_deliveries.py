"""Public, no-auth access to delivered reports (link + file export + receipt)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.models.report_delivery import RECIPIENT_KIND_SCHOOL, RECIPIENT_KIND_STANDARD
from app.schemas.report_delivery import (
    PublicReportDeliveryResponse,
    ReportReceiptCreate,
    ReportReceiptResponse,
)
from app.services import clinical_public_rate_limit, school_report_delivery_service
from app.services.professional_branding import build_document_identity
from app.services.report_delivery_service import (
    InvalidReportDeliveryToken,
    apply_snapshot_identity,
    hash_delivery_token,
    load_public_delivery,
    register_delivery_download,
    register_delivery_view,
    resolve_public_document,
)
from app.services.report_export import (
    REPORT_TYPE_LABELS,
    export_report,
    sanitize_filename_component,
)

router = APIRouter(prefix="/report-deliveries", tags=["report-deliveries"])


def _recipient_kind(delivery) -> str:
    return delivery.recipient_kind or RECIPIENT_KIND_STANDARD


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
    document = resolve_public_document(context)
    recipient_kind = _recipient_kind(context.delivery)
    return PublicReportDeliveryResponse(
        report_type=document.report_type,
        report_type_label=REPORT_TYPE_LABELS.get(document.report_type, document.report_type),
        patient_name=document.patient_name,
        professional_name=document.professional_name,
        professional_council=document.professional_council,
        date=document.report_date.isoformat(),
        content=document.content,
        expires_at=context.delivery.expires_at,
        report_version=document.report_version,
        content_hash=document.content_hash,
        snapshot_mode=document.snapshot_mode,
        recipient_kind=recipient_kind,
        requires_acknowledgement=recipient_kind == RECIPIENT_KIND_SCHOOL,
        received_at=context.delivery.received_at,
    )


@router.post("/{token}/acknowledgement", response_model=ReportReceiptResponse)
async def acknowledge_public_report_delivery(
    token: str,
    body: ReportReceiptCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """F20 — confirmação pública de recebimento (idempotente, sob lock).

    O rate limit público (token hasheado + IP confiável) roda antes do banco; o
    recibo é gravado sob o lock da entrega e a resposta só sai após o commit.
    """
    raw_token = (token or "").strip()
    await run_in_threadpool(
        clinical_public_rate_limit.enforce_report_delivery_acknowledgement_rate_limit,
        request,
        token_hash=hash_delivery_token(raw_token),
    )
    try:
        receipt = await school_report_delivery_service.acknowledge_school_delivery(
            db, token=raw_token, body=body
        )
    except InvalidReportDeliveryToken as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    await db.commit()
    return receipt


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
    document = resolve_public_document(context)
    identity = apply_snapshot_identity(
        await build_document_identity(context.professional), document
    )
    try:
        data, media_type, suffix = export_report(
            format,
            document.report_type,
            document.patient_name,
            document.report_date,
            document.content,
            identity=identity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await register_delivery_download(db, context.delivery)
    filename = (
        f"relatorio-{sanitize_filename_component(document.report_type)}"
        f"-{document.report_date.isoformat()}.{suffix}"
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
