from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIReport, AIReportRevision
from app.schemas.ai import AIReportUpdate


async def revise_report(db: AsyncSession, report_id: UUID, professional_id: UUID, body: AIReportUpdate) -> AIReport:
    report = await db.scalar(select(AIReport).where(
        AIReport.id == report_id, AIReport.professional_id == professional_id,
    ).with_for_update().execution_options(populate_existing=True))
    if report is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    next_status = body.status or "finalized"
    if report.status == "finalized" and next_status == "draft":
        raise HTTPException(status_code=409, detail="Um relatório finalizado não pode voltar a rascunho. Salve uma revisão.")
    if report.content != body.content or report.status != next_status:
        db.add(AIReportRevision(report_id=report.id, professional_id=professional_id,
            content=report.content, status=report.status))
        report.content = body.content
        report.preview = body.content[:200] + ("..." if len(body.content) > 200 else "")
        report.status = next_status
        await db.flush()
    return report
