"""Revision of AI reports with numeric versioning and optimistic control (F3)."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIReport, AIReportRevision
from app.models.patient import Patient
from app.schemas.ai import AIReportUpdate

CONSOLIDATED_REPORT_TYPE = "consolidado"
# Fixed top-level sections of the consolidated template; the editor protects the
# deterministic ones and the API rejects content that no longer matches.
CONSOLIDATED_SECTIONS = ("## Identificação", "## Instrumentos", "## Síntese", "## Conduta")


def _require_consolidated_payload(body: AIReportUpdate) -> None:
    if body.expected_version is None:
        raise HTTPException(
            status_code=422,
            detail="Informe expectedVersion ao salvar um laudo consolidado.",
        )
    if body.status is None:
        raise HTTPException(
            status_code=422,
            detail="Informe o status (draft ou finalized) ao salvar um laudo consolidado.",
        )


def _require_consolidated_sections(content: str) -> None:
    lines = [line.strip() for line in (content or "").splitlines()]
    missing = [heading for heading in CONSOLIDATED_SECTIONS if heading not in lines]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "O laudo consolidado deve manter as quatro seções Identificação, "
                "Instrumentos, Síntese e Conduta (faltando: " + ", ".join(missing) + ")."
            ),
        )
    positions = [lines.index(heading) for heading in CONSOLIDATED_SECTIONS]
    if positions != sorted(positions):
        raise HTTPException(
            status_code=422,
            detail=(
                "As seções do laudo consolidado devem seguir a ordem "
                "Identificação, Instrumentos, Síntese e Conduta."
            ),
        )


async def revise_report(db: AsyncSession, report_id: UUID, professional_id: UUID, body: AIReportUpdate) -> AIReport:
    # The row lock serializes concurrent revisions; it is taken only here, at the
    # persistence boundary — never while an LLM call is running.
    report = await db.scalar(select(AIReport).where(
        AIReport.id == report_id, AIReport.professional_id == professional_id,
    ).with_for_update().execution_options(populate_existing=True))
    if report is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")

    if report.type == CONSOLIDATED_REPORT_TYPE:
        _require_consolidated_payload(body)
        _require_consolidated_sections(body.content)

    current_version = report.version if report.version is not None else 1
    if body.expected_version is not None and body.expected_version != current_version:
        raise HTTPException(
            status_code=409,
            detail="O relatório foi alterado por outra sessão. Recarregue antes de salvar.",
        )

    is_demo = await db.scalar(select(Patient.is_demo).where(Patient.id == report.patient_id))
    next_status = body.status or (report.status if is_demo else "finalized")
    if report.status == "finalized" and next_status == "draft":
        raise HTTPException(status_code=409, detail="Um relatório finalizado não pode voltar a rascunho. Salve uma revisão.")
    if report.content != body.content or report.status != next_status:
        db.add(AIReportRevision(report_id=report.id, professional_id=professional_id,
            content=report.content, status=report.status, version=current_version))
        report.content = body.content
        report.preview = body.content[:200] + ("..." if len(body.content) > 200 else "")
        report.status = next_status
        report.version = current_version + 1
        await db.flush()
    return report
