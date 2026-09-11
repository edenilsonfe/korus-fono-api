"""F6 — coleta, render e auditoria das exportações do prontuário.

O resumo B (``GET /patients/{id}/export.pdf``) é o documento fixo e minimizado
da fase 3.1. O dossiê selecionável (``POST /patients/{id}/record-exports``) é
uma segunda ação, claramente separada: identificação obrigatória e seções
opcionais, período em datas locais da clínica (inclusivo), limites duros
(rejeitar, nunca truncar), marca d'água em todas as páginas geradas e auditoria.

A auditoria é um registro próprio, não um evento de timeline clínica: a
tentativa autorizada vira ``requested`` numa transação antes da geração, e o
resultado (``generated``/``failed``) é gravado numa segunda transação
independente. Se o processo cair no meio, o ``requested`` permanece visível —
nunca um sucesso inventado, e sem duplicar conteúdo clínico no log.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import weakref
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.diagnosis_catalog import diagnosis_labels
from app.core.utils import calculate_age
from app.db.session import AsyncSessionLocal
from app.models.anamnese import AnamneseEntry
from app.models.assessment import (
    ASSESSMENT_STATUS_COMPLETED,
    Assessment,
    ProtocolCatalog,
)
from app.models.attachment import Attachment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.patient_record_export import (
    EXPORT_FORMAT_PDF,
    EXPORT_FORMAT_ZIP,
    EXPORT_KIND_DOSSIER,
    EXPORT_KIND_SUMMARY,
    EXPORT_PURPOSE_CARE_CONTINUITY,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_GENERATED,
    EXPORT_STATUS_REQUESTED,
    PatientRecordExport,
)
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.patient_export import PatientRecordExportRequest
from app.services.attachment_upload import sanitize_filename
from app.services.patient_summary_export import (
    escape_paragraph_text,
    export_patient_summary_pdf,
)
from app.services.professional_branding import build_document_identity
from app.services.report_export import DocumentIdentity
from app.services.storage import StorageLimitExceededError, storage_service

logger = logging.getLogger(__name__)

# Seleção fixa do resumo B (design B do spike 017): identificação/diagnósticos,
# metas atuais e últimas sessões. Nenhum parâmetro amplia o documento.
SUMMARY_SECTIONS: tuple[str, ...] = ("identification", "goals", "sessions")
SUMMARY_PURPOSE = EXPORT_PURPOSE_CARE_CONTINUITY

ERROR_CODE_RENDER_FAILED = "render_failed"
ERROR_CODE_EXPORT_TIMEOUT = "export_timeout"
ERROR_CODE_RECORD_LIMIT = "record_limit_exceeded"
ERROR_CODE_TEXT_LIMIT = "text_limit_exceeded"
ERROR_CODE_PAGE_LIMIT = "page_limit_exceeded"
ERROR_CODE_ATTACHMENT_LIMIT = "attachment_limit_exceeded"
ERROR_CODE_STORAGE_UNAVAILABLE = "storage_unavailable"
ERROR_CODE_REQUEST_REJECTED = "request_rejected"

AUDIT_UNAVAILABLE_DETAIL = (
    "Não foi possível registrar a exportação do prontuário. Tente novamente."
)
EXPORT_FAILED_DETAIL = "Não foi possível gerar o resumo do paciente. Tente novamente."
DOSSIER_FAILED_DETAIL = "Não foi possível gerar o dossiê do paciente. Tente novamente."
DOSSIER_STORAGE_DETAIL = (
    "Não foi possível ler os anexos selecionados. Nenhum arquivo foi gerado; "
    "tente novamente."
)
DOSSIER_TIMEOUT_DETAIL = (
    "Tempo esgotado ao gerar o dossiê do paciente. Reduza a seleção e tente novamente."
)

# --------------------------------------------------------------------------- #
# Dossiê selecionável (F6, fase 2)
# --------------------------------------------------------------------------- #

# Limites duros do §3.3: rejeitar a seleção excedente, nunca cortar o conteúdo.
MAX_EXPORT_ATTACHMENTS = 20
MAX_EXPORT_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_EXPORT_DATED_RECORDS = 500
MAX_EXPORT_TEXT_CHARS = 200_000
MAX_EXPORT_PAGES = 200

# Render síncrono fora do event loop, com concorrência limitada e orçamento de
# operação (API 60 s). O semáforo limita o dano de renderizações simultâneas; o
# timeout controla a resposta — cancelar a thread não é instantâneo.
RENDER_CONCURRENCY = 2
RENDER_TIMEOUT_SECONDS = 45.0
ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS = 20.0
# Orçamento total da operação síncrona (sem ARQ). O web usa orçamento próprio.
EXPORT_BUDGET_SECONDS = 60.0

CANONICAL_SECTION_ORDER: tuple[str, ...] = (
    "identification",
    "anamnesis",
    "assessments",
    "evolutions",
    "goals",
    "sessions",
    "attachments",
)

DOSSIER_DOCUMENT_LABEL = "Dossiê do prontuário"
DOSSIER_PURPOSE_NOTE = (
    "Documento gerado a partir dos dados salvos do prontuário, conforme a "
    "seleção e o período informados. Não é portabilidade jurídica integral e "
    "não inclui registros não selecionados."
)
WATERMARK_TEXT = "CONFIDENCIAL — Dados de saúde"
DEMO_WATERMARK_TEXT = "DEMONSTRAÇÃO"
ATTACHMENT_INDEX_NOTE = (
    "Somente a exportação em ZIP inclui os arquivos originais; os originais "
    "saem sem marca d'água e sem transformação."
)
EMPTY_SECTION_LABELS = {
    "anamnesis": "Nenhum registro de anamnese.",
    "assessments": "Nenhuma avaliação concluída no período.",
    "evolutions": "Nenhuma evolução no período.",
    "goals": "Nenhuma meta registrada.",
    "sessions": "Nenhuma sessão no período.",
    "attachments": "Nenhum anexo selecionado.",
}
PURPOSE_LABELS = {
    "care_continuity": "Continuidade do cuidado",
    "patient_request": "Solicitação do paciente/responsável",
    "professional_archive": "Arquivo profissional",
}

DOSSIER_PDF_ENTRY = "prontuario.pdf"
DOSSIER_MANIFEST_ENTRY = "manifest.json"
DOSSIER_MANIFEST_VERSION = 1
ATTACHMENT_ENTRY_PREFIX = "anexos"
# O manifesto nunca carrega chave de storage, URL assinada ou token — apenas
# identificadores, nomes, tamanhos, hashes e contagens.
MANIFEST_NOTICE = (
    "Os arquivos em anexos/ são os originais enviados, sem marca d'água nem "
    "transformação."
)

MEDIA_TYPES = {
    EXPORT_FORMAT_PDF: "application/pdf",
    EXPORT_FORMAT_ZIP: "application/zip",
}


class _ExportLimitExceeded(Exception):
    """Limite duro do dossiê excedido — rejeitar a seleção, nunca truncar."""

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str = "",
        detail: str = "",
        status_code: int = 422,
    ):
        # ``message`` posicional existe porque o ReportLab pode reencapsular a
        # exceção (``annotateException``) ao propagá-la de um callback.
        super().__init__(message or detail)
        self.error_code = error_code
        self.detail = detail or message
        self.status_code = status_code

    def as_http_exception(self) -> HTTPException:
        return HTTPException(status_code=self.status_code, detail=self.detail)


class _DossierStorageUnavailable(Exception):
    """Storage de anexos indisponível/objeto ausente: sem pacote parcial."""


def _attachment_count_detail() -> str:
    return f"Selecione no máximo {MAX_EXPORT_ATTACHMENTS} anexos por exportação."


def _attachment_bytes_detail() -> str:
    if (
        MAX_EXPORT_ATTACHMENT_BYTES >= 1024 * 1024
        and MAX_EXPORT_ATTACHMENT_BYTES % (1024 * 1024) == 0
    ):
        limit = f"{MAX_EXPORT_ATTACHMENT_BYTES // (1024 * 1024)} MiB"
    else:
        limit = f"{MAX_EXPORT_ATTACHMENT_BYTES} bytes"
    return f"Os anexos selecionados excedem o limite agregado de {limit}."


def _record_limit_detail() -> str:
    return (
        f"O dossiê excede o limite de {MAX_EXPORT_DATED_RECORDS} registros "
        "datados. Reduza o período ou as seções."
    )


def _text_limit_detail() -> str:
    limit = f"{MAX_EXPORT_TEXT_CHARS:_}".replace("_", ".")
    return f"O dossiê excede o limite de {limit} caracteres de texto. Reduza a seleção."


def _page_limit_detail() -> str:
    return (
        f"O dossiê excede o limite de {MAX_EXPORT_PAGES} páginas. "
        "Reduza a seleção."
    )


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


async def collect_patient_summary_data(
    db: AsyncSession, *, patient_id: UUID, sessions_limit: int
) -> tuple[list[Goal], list[Session]]:
    """Metas atuais e as últimas N sessões (data/id decrescente)."""
    goals = list(
        (
            await db.execute(
                select(Goal)
                .where(Goal.patient_id == patient_id)
                .order_by(Goal.start_date.desc(), Goal.id.desc())
            )
        ).scalars()
    )
    sessions = list(
        (
            await db.execute(
                select(Session)
                .where(Session.patient_id == patient_id)
                .order_by(Session.date.desc(), Session.id.desc())
                .limit(sessions_limit)
            )
        ).scalars()
    )
    return goals, sessions


async def begin_patient_record_export(
    *,
    patient_id: UUID,
    professional_id: UUID,
    kind: str,
    export_format: str,
    sections: Sequence[str],
    purpose: str,
    from_date: date | None = None,
    to_date: date | None = None,
    attachment_count: int | None = None,
    requested_at: datetime | None = None,
) -> UUID:
    """Persiste a tentativa autorizada (``requested``) em transação própria.

    É commitada antes da geração: uma queda do processo deixa a tentativa
    visível, sem registrar resultado nenhum.
    """
    async with AsyncSessionLocal() as session:
        row = PatientRecordExport(
            patient_id=patient_id,
            professional_id=professional_id,
            kind=kind,
            format=export_format,
            sections=list(sections),
            from_date=from_date,
            to_date=to_date,
            purpose=purpose,
            status=EXPORT_STATUS_REQUESTED,
            requested_at=requested_at or datetime.now(UTC),
            attachment_count=attachment_count,
        )
        session.add(row)
        await session.commit()
        return row.id


async def finalize_patient_record_export(
    export_id: UUID,
    *,
    result_status: str,
    record_counts: dict | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    error_code: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Grava o resultado numa segunda transação independente."""
    async with AsyncSessionLocal() as session:
        row = await session.get(PatientRecordExport, export_id)
        if row is None:
            raise LookupError(f"Exportação {export_id} não encontrada para auditoria")
        row.status = result_status
        row.completed_at = completed_at or datetime.now(UTC)
        if record_counts is not None:
            row.record_counts = record_counts
        if size_bytes is not None:
            row.size_bytes = size_bytes
        if sha256 is not None:
            row.sha256 = sha256
        if error_code is not None:
            row.error_code = error_code
        await session.commit()


async def _finalize_failure(export_id: UUID, *, error_code: str) -> None:
    """Marca a falha sem mascarar o erro original: se nem isso persistir, o
    ``requested`` continua sendo o estado visível — a falha não vira sucesso."""
    try:
        await finalize_patient_record_export(
            export_id, result_status=EXPORT_STATUS_FAILED, error_code=error_code
        )
    except Exception:  # noqa: BLE001 - o 503 original prevalece
        logger.exception("Failed to persist patient record export failure audit")


async def generate_patient_summary_export(
    db: AsyncSession,
    *,
    patient: Patient,
    professional: Professional,
    sessions_limit: int,
) -> bytes:
    """Resumo B ponta a ponta: coleta, auditoria ``requested``, render, resultado."""
    goals, sessions = await collect_patient_summary_data(
        db, patient_id=patient.id, sessions_limit=sessions_limit
    )
    requested_at = datetime.now(UTC)
    try:
        export_id = await begin_patient_record_export(
            patient_id=patient.id,
            professional_id=professional.id,
            kind=EXPORT_KIND_SUMMARY,
            export_format=EXPORT_FORMAT_PDF,
            sections=SUMMARY_SECTIONS,
            purpose=SUMMARY_PURPOSE,
            attachment_count=0,
            requested_at=requested_at,
        )
    except Exception as exc:
        logger.exception("Failed to persist patient record export audit request")
        raise _unavailable(AUDIT_UNAVAILABLE_DETAIL) from exc

    try:
        identity = await build_document_identity(professional)
        pdf = export_patient_summary_pdf(
            patient=patient,
            goals=goals,
            sessions=sessions,
            specialty_key=professional.specialty_key,
            identity=identity,
            generated_at=requested_at,
        )
    except Exception as exc:
        logger.exception("Failed to render patient summary PDF")
        await _finalize_failure(export_id, error_code=ERROR_CODE_RENDER_FAILED)
        raise _unavailable(EXPORT_FAILED_DETAIL) from exc

    try:
        await finalize_patient_record_export(
            export_id,
            result_status=EXPORT_STATUS_GENERATED,
            record_counts={"goals": len(goals), "sessions": len(sessions)},
            size_bytes=len(pdf),
            sha256=hashlib.sha256(pdf).hexdigest(),
        )
    except Exception as exc:
        logger.exception("Failed to persist patient record export audit result")
        raise _unavailable(AUDIT_UNAVAILABLE_DETAIL) from exc
    return pdf


# --------------------------------------------------------------------------- #
# Dossiê selecionável — coleta
# --------------------------------------------------------------------------- #


@dataclass
class DossierRecords:
    """Dados selecionados do prontuário, já filtrados e ordenados.

    Nunca carrega relacionamentos lazy: os nomes de protocolo/autoria são
    resolvidos na coleta e o render roda fora da sessão (thread).
    """

    sections: list[str]
    from_date: date | None
    to_date: date | None
    anamnesis_status: str
    anamnesis_entries: list[AnamneseEntry] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)
    protocol_names: dict[str, str] = field(default_factory=dict)
    evolutions: list[Evolution] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    author_names: dict[UUID, str] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """Contagens por seção incluída (para auditoria/manifesto)."""
        counted = {
            "anamnesis": self.anamnesis_entries,
            "assessments": self.assessments,
            "evolutions": self.evolutions,
            "goals": self.goals,
            "sessions": self.sessions,
            "attachments": self.attachments,
        }
        return {
            section: len(rows)
            for section, rows in counted.items()
            if section in self.sections
        }

    @property
    def dated_record_count(self) -> int:
        return len(self.assessments) + len(self.evolutions) + len(self.sessions)

    @property
    def text_char_count(self) -> int:
        total = 0
        for entry in self.anamnesis_entries:
            total += len(entry.value or "")
        for evolution in self.evolutions:
            total += len(evolution.content or "") + len(evolution.title or "")
        for goal in self.goals:
            total += len(goal.title or "") + len(goal.area or "") + len(goal.status or "")
        for session in self.sessions:
            total += len(session.type or "")
        return total


def _clinic_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().clinic_timezone)


def clinic_period_bounds(
    from_date: date | None,
    to_date: date | None,
    *,
    timezone: ZoneInfo | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Datas locais inclusivas da clínica → instante UTC ``[start, end)``.

    ``from``/``to`` são datas locais; registros com timestamp são filtrados
    pelo intervalo convertido (o dia final entra inteiro).
    """
    tz = timezone or _clinic_timezone()
    start = (
        datetime.combine(from_date, time.min, tzinfo=tz).astimezone(UTC)
        if from_date
        else None
    )
    end = (
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
        if to_date
        else None
    )
    return start, end


async def _protocol_names(db: AsyncSession, protocol_ids: list[str]) -> dict[str, str]:
    if not protocol_ids:
        return {}
    rows = (
        await db.execute(
            select(ProtocolCatalog.id, ProtocolCatalog.name).where(
                ProtocolCatalog.id.in_(set(protocol_ids))
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _professional_names(db: AsyncSession, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Professional.id, Professional.name).where(Professional.id.in_(ids))
        )
    ).all()
    return {row[0]: (row[1] or "").strip() for row in rows}


async def collect_patient_record_export(
    db: AsyncSession,
    *,
    patient: Patient,
    request: PatientRecordExportRequest,
    attachments: Sequence[Attachment] | None = None,
) -> DossierRecords:
    """Carrega somente o que foi selecionado, no período local da clínica.

    Metas e anamnese são estado atual (não têm corte histórico); avaliações,
    evoluções, sessões e anexos são filtrados por suas datas. Sessões ligadas a
    uma evolução incluída não se repetem (``sessionId``).
    """
    sections = [section for section in CANONICAL_SECTION_ORDER if section in request.sections]
    records = DossierRecords(
        sections=sections,
        from_date=request.from_date,
        to_date=request.to_date,
        anamnesis_status=(patient.anamnese_status or "draft"),
        attachments=list(attachments or []),
    )
    start, end = clinic_period_bounds(request.from_date, request.to_date)

    if "anamnesis" in sections:
        records.anamnesis_entries = list(
            (
                await db.execute(
                    select(AnamneseEntry)
                    .where(AnamneseEntry.patient_id == patient.id)
                    .order_by(AnamneseEntry.section.asc(), AnamneseEntry.id.asc())
                )
            ).scalars()
        )

    if "assessments" in sections:
        filters = [
            Assessment.patient_id == patient.id,
            Assessment.status == ASSESSMENT_STATUS_COMPLETED,
        ]
        if request.from_date is not None:
            filters.append(Assessment.date >= request.from_date)
        if request.to_date is not None:
            filters.append(Assessment.date <= request.to_date)
        rows = list(
            (
                await db.execute(
                    select(Assessment)
                    .where(*filters)
                    .order_by(Assessment.date.asc(), Assessment.id.asc())
                    .limit(MAX_EXPORT_DATED_RECORDS + 1)
                )
            ).scalars()
        )
        # "Resultados persistidos": sem resultado salvo não há o que exportar.
        records.assessments = [row for row in rows if (row.result or "").strip()]

    if "evolutions" in sections:
        filters = [Evolution.patient_id == patient.id]
        if start is not None:
            filters.append(Evolution.date >= start)
        if end is not None:
            filters.append(Evolution.date < end)
        records.evolutions = list(
            (
                await db.execute(
                    select(Evolution)
                    .where(*filters)
                    .order_by(Evolution.date.asc(), Evolution.id.asc())
                    .limit(MAX_EXPORT_DATED_RECORDS + 1)
                )
            ).scalars()
        )

    if "goals" in sections:
        records.goals = list(
            (
                await db.execute(
                    select(Goal)
                    .where(Goal.patient_id == patient.id)
                    .order_by(Goal.start_date.desc(), Goal.id.desc())
                )
            ).scalars()
        )

    if "sessions" in sections:
        filters = [Session.patient_id == patient.id]
        if start is not None:
            filters.append(Session.date >= start)
        if end is not None:
            filters.append(Session.date < end)
        sessions = list(
            (
                await db.execute(
                    select(Session)
                    .where(*filters)
                    .order_by(Session.date.asc(), Session.id.asc())
                    .limit(MAX_EXPORT_DATED_RECORDS + 1)
                )
            ).scalars()
        )
        if "evolutions" in sections:
            represented = {
                evolution.session_id
                for evolution in records.evolutions
                if evolution.session_id is not None
            }
            sessions = [session for session in sessions if session.id not in represented]
        records.sessions = sessions

    author_ids = {evolution.professional_id for evolution in records.evolutions}
    author_ids.add(patient.professional_id)
    records.author_names = await _professional_names(db, author_ids)
    records.protocol_names = await _protocol_names(
        db, [assessment.protocol_id for assessment in records.assessments]
    )
    return records


def _assert_dossier_limits(records: DossierRecords) -> None:
    if records.dated_record_count > MAX_EXPORT_DATED_RECORDS:
        raise _ExportLimitExceeded(
            error_code=ERROR_CODE_RECORD_LIMIT, detail=_record_limit_detail()
        )
    if records.text_char_count > MAX_EXPORT_TEXT_CHARS:
        raise _ExportLimitExceeded(
            error_code=ERROR_CODE_TEXT_LIMIT, detail=_text_limit_detail()
        )


async def _load_selected_attachments(
    db: AsyncSession, *, patient_id: UUID, attachment_ids: Sequence[UUID]
) -> list[Attachment]:
    """Resolve e valida a seleção de anexos antes de auditar a tentativa.

    ``[]`` nunca significa "todos"; ID fora do escopo do paciente é 404 e a
    seleção acima dos limites é rejeitada (413) sem gerar arquivo.
    """
    if not attachment_ids:
        return []
    if len(set(attachment_ids)) != len(attachment_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="attachmentIds não pode conter duplicatas.",
        )
    rows = list(
        (
            await db.execute(
                select(Attachment).where(
                    Attachment.patient_id == patient_id,
                    Attachment.id.in_(list(attachment_ids)),
                )
            )
        ).scalars()
    )
    by_id = {row.id: row for row in rows}
    if any(attachment_id not in by_id for attachment_id in attachment_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anexo não encontrado"
        )
    selected = [by_id[attachment_id] for attachment_id in attachment_ids]
    if len(selected) > MAX_EXPORT_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=_attachment_count_detail(),
        )
    declared_bytes = sum(int(attachment.size_bytes or 0) for attachment in selected)
    if declared_bytes > MAX_EXPORT_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=_attachment_bytes_detail(),
        )
    return selected


# --------------------------------------------------------------------------- #
# Dossiê selecionável — render PDF
# --------------------------------------------------------------------------- #


def _image_flowable(data: bytes | None, *, width: float) -> Image | None:
    if not data:
        return None
    try:
        reader = ImageReader(io.BytesIO(data))
        original_width, original_height = reader.getSize()
        if not original_width or not original_height:
            return None
        return Image(
            io.BytesIO(data), width=width, height=width * original_height / original_width
        )
    except Exception:  # noqa: BLE001 - imagem quebrada não quebra o documento
        return None


def _identity_headline(identity: DocumentIdentity | None) -> str:
    if identity is None:
        return ""
    name = (identity.professional_name or "").strip()
    council = (identity.council or "").strip()
    if name and council:
        return f"{name} — {council}"
    return name or council


def _format_bytes(size_bytes: int | None) -> str:
    total = int(size_bytes or 0)
    if total < 1024:
        return f"{total} bytes"
    for unit, divisor in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if total >= divisor:
            return f"{total / divisor:.1f} {unit}"
    return f"{total} bytes"


def _period_label(records: DossierRecords) -> str:
    if records.from_date and records.to_date:
        return (
            f"{records.from_date.strftime('%d/%m/%Y')} a "
            f"{records.to_date.strftime('%d/%m/%Y')}"
        )
    if records.from_date:
        return f"a partir de {records.from_date.strftime('%d/%m/%Y')}"
    if records.to_date:
        return f"até {records.to_date.strftime('%d/%m/%Y')}"
    return "todo o histórico"


def _draw_confidential_watermark(
    canvas, *, patient: Patient, export_id: UUID, when: datetime, page_number: int
) -> None:
    """Marca d'água em TODA página + identificador/data/paginação."""
    width, height = A4
    canvas.saveState()
    try:
        canvas.setFillColor(colors.Color(0.62, 0.62, 0.62))
        canvas.setFont("Helvetica-Bold", 38)
        canvas.translate(width / 2, height / 2)
        canvas.rotate(32)
        canvas.drawCentredString(0, 0, WATERMARK_TEXT)
        if patient.is_demo:
            canvas.setFont("Helvetica-Bold", 30)
            canvas.drawCentredString(0, -58, DEMO_WATERMARK_TEXT)
    finally:
        canvas.restoreState()

    canvas.saveState()
    try:
        canvas.setFillColor(colors.Color(0.42, 0.42, 0.42))
        canvas.setFont("Helvetica", 8)
        canvas.drawCentredString(
            width / 2,
            22,
            f"Exportação {export_id} · {when.strftime('%d/%m/%Y')} · Página {page_number}",
        )
    finally:
        canvas.restoreState()


class _PageCounter:
    """Conta as páginas geradas pelo callback do ReportLab (sem exceção no build)."""

    __slots__ = ("pages",)

    def __init__(self) -> None:
        self.pages = 0


def _page_decorator(*, patient: Patient, export_id: UUID, when: datetime, counter: _PageCounter):
    def decorate(canvas, doc) -> None:
        counter.pages = canvas.getPageNumber()
        _draw_confidential_watermark(
            canvas,
            patient=patient,
            export_id=export_id,
            when=when,
            page_number=counter.pages,
        )

    return decorate


def _append_identification(
    story, styles, patient: Patient, specialty_key: str, today: date
) -> None:
    story.append(Paragraph("Identificação", styles["Heading2"]))
    keys = patient.diagnosis_keys or []
    labels = diagnosis_labels(keys, specialty_key)
    diagnoses = (
        "; ".join(escape_paragraph_text(label) for label in labels) or "Não informados"
    )
    age = calculate_age(patient.birth_date, today)
    story.append(Paragraph(f"• Nome: {escape_paragraph_text(patient.name)}", styles["Normal"]))
    story.append(
        Paragraph(
            f"• Nascimento: {patient.birth_date.strftime('%d/%m/%Y')} ({age} anos)",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"• Status: {escape_paragraph_text(patient.status)} · Início do "
            f"acompanhamento: {patient.start_date.strftime('%d/%m/%Y')}",
            styles["Normal"],
        )
    )
    story.append(Paragraph(f"• Diagnósticos: {diagnoses}", styles["Normal"]))
    story.append(Spacer(1, 6))


def _append_anamnesis(story, styles, records: DossierRecords) -> None:
    is_draft = records.anamnesis_status != "completed"
    label = "Anamnese (rascunho — não concluída)" if is_draft else "Anamnese"
    story.append(Paragraph(label, styles["Heading2"]))
    if not records.anamnesis_entries:
        story.append(Paragraph(EMPTY_SECTION_LABELS["anamnesis"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for entry in records.anamnesis_entries:
        story.append(
            Paragraph(
                f"• {escape_paragraph_text(entry.section)}: "
                f"{escape_paragraph_text(entry.value)}",
                styles["Normal"],
            )
        )
    story.append(Spacer(1, 6))


def _append_assessments(story, styles, records: DossierRecords) -> None:
    story.append(Paragraph("Avaliações (somente concluídas)", styles["Heading2"]))
    if not records.assessments:
        story.append(Paragraph(EMPTY_SECTION_LABELS["assessments"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for assessment in records.assessments:
        label = records.protocol_names.get(assessment.protocol_id) or assessment.protocol_id
        story.append(
            Paragraph(
                f"• {assessment.date.strftime('%d/%m/%Y')} — "
                f"{escape_paragraph_text(label)}: "
                f"{escape_paragraph_text((assessment.result or '').strip())} "
                f"({assessment.percentage}%)",
                styles["Normal"],
            )
        )
        interpretation = (assessment.interpretation or "").strip()
        if interpretation:
            story.append(Paragraph(escape_paragraph_text(interpretation), styles["Normal"]))
    story.append(Spacer(1, 6))


def _append_evolutions(story, styles, records: DossierRecords) -> None:
    story.append(Paragraph("Evoluções", styles["Heading2"]))
    if not records.evolutions:
        story.append(Paragraph(EMPTY_SECTION_LABELS["evolutions"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for evolution in records.evolutions:
        parts = [evolution.date.strftime("%d/%m/%Y %H:%M")]
        author = records.author_names.get(evolution.professional_id)
        if author:
            parts.append(escape_paragraph_text(author))
        title = (evolution.title or "").strip()
        if title:
            parts.append(escape_paragraph_text(title))
        story.append(Paragraph(f"• {' — '.join(parts)}", styles["Normal"]))
        content = (evolution.content or "").strip()
        if content:
            story.append(Paragraph(escape_paragraph_text(content), styles["Normal"]))
    story.append(Spacer(1, 6))


def _append_goals(story, styles, records: DossierRecords) -> None:
    story.append(Paragraph("Metas (estado atual)", styles["Heading2"]))
    if not records.goals:
        story.append(Paragraph(EMPTY_SECTION_LABELS["goals"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for goal in records.goals:
        parts = [escape_paragraph_text((goal.title or "").strip())]
        area = (goal.area or "").strip()
        if area:
            parts.append(escape_paragraph_text(area))
        headline = " — ".join(part for part in parts if part)
        story.append(
            Paragraph(
                f"• {headline} — {goal.progress}% — "
                f"{escape_paragraph_text((goal.status or '').strip())}",
                styles["Normal"],
            )
        )
    story.append(Spacer(1, 6))


def _append_sessions(story, styles, records: DossierRecords) -> None:
    story.append(Paragraph("Sessões", styles["Heading2"]))
    if not records.sessions:
        story.append(Paragraph(EMPTY_SECTION_LABELS["sessions"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for session in records.sessions:
        story.append(
            Paragraph(
                f"• {session.date.strftime('%d/%m/%Y %H:%M')} — "
                f"{escape_paragraph_text((session.type or '').strip())}",
                styles["Normal"],
            )
        )
    story.append(Spacer(1, 6))


def _append_attachments_index(story, styles, records: DossierRecords) -> None:
    story.append(
        Paragraph("Anexos (índice — os arquivos originais não estão neste PDF)", styles["Heading2"])
    )
    if not records.attachments:
        story.append(Paragraph(EMPTY_SECTION_LABELS["attachments"], styles["Normal"]))
        story.append(Spacer(1, 6))
        return
    for attachment in records.attachments:
        story.append(
            Paragraph(
                f"• {escape_paragraph_text(attachment.name)} — "
                f"{escape_paragraph_text(attachment.category)} — "
                f"{attachment.date.strftime('%d/%m/%Y')} — "
                f"{_format_bytes(attachment.size_bytes)} — ID {attachment.id}",
                styles["Normal"],
            )
        )
    story.append(Paragraph(ATTACHMENT_INDEX_NOTE, styles["Italic"]))
    story.append(Spacer(1, 6))


def render_patient_record_pdf(
    *,
    patient: Patient,
    records: DossierRecords,
    export_id: UUID,
    purpose: str,
    specialty_key: str = "fono",
    identity: DocumentIdentity | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """Renderiza o dossiê selecionado (síncrono; chamado fora do event loop)."""
    when = generated_at or datetime.now(UTC)
    styles = getSampleStyleSheet()
    story: list = []

    logo = _image_flowable(identity.logo_bytes if identity else None, width=110)
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 8))
    story.extend(
        [
            Paragraph("KorusFono", styles["Title"]),
            Paragraph(DOSSIER_DOCUMENT_LABEL, styles["Heading2"]),
            Paragraph(f"Paciente: {escape_paragraph_text(patient.name)}", styles["Normal"]),
            Paragraph(f"Data: {when.date().isoformat()}", styles["Normal"]),
            Paragraph(f"Exportação: {export_id}", styles["Normal"]),
            Paragraph(
                f"Finalidade: {PURPOSE_LABELS.get(purpose, purpose)}", styles["Normal"]
            ),
            Paragraph(f"Período: {_period_label(records)}", styles["Normal"]),
        ]
    )
    headline = _identity_headline(identity)
    if headline:
        story.append(
            Paragraph(f"Profissional: {escape_paragraph_text(headline)}", styles["Normal"])
        )
    story.append(Paragraph(DOSSIER_PURPOSE_NOTE, styles["Italic"]))
    story.append(Spacer(1, 12))

    for section in records.sections:
        if section == "identification":
            _append_identification(story, styles, patient, specialty_key, when.date())
        elif section == "anamnesis":
            _append_anamnesis(story, styles, records)
        elif section == "assessments":
            _append_assessments(story, styles, records)
        elif section == "evolutions":
            _append_evolutions(story, styles, records)
        elif section == "goals":
            _append_goals(story, styles, records)
        elif section == "sessions":
            _append_sessions(story, styles, records)
        elif section == "attachments":
            _append_attachments_index(story, styles, records)

    if headline:
        story.append(Spacer(1, 24))
        signature = _image_flowable(identity.signature_bytes if identity else None, width=140)
        if signature is not None:
            story.append(signature)
            story.append(Spacer(1, 4))
        story.append(Paragraph(escape_paragraph_text(headline), styles["Normal"]))
        story.append(Paragraph(f"Emitido em {when.date().isoformat()}", styles["Normal"]))

    counter = _PageCounter()
    decorate = _page_decorator(
        patient=patient, export_id=export_id, when=when, counter=counter
    )
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    # O limite de páginas é checado após o build (o ReportLab reencapsula
    # exceções de callback): o documento excedente é rejeitado, nunca servido
    # truncado. O volume já está limitado por registros/caracteres.
    if counter.pages > MAX_EXPORT_PAGES:
        raise _ExportLimitExceeded(
            error_code=ERROR_CODE_PAGE_LIMIT, detail=_page_limit_detail()
        )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Dossiê selecionável — ZIP + manifesto
# --------------------------------------------------------------------------- #


def attachment_entry_name(attachment: Attachment) -> tuple[str, str]:
    """``(entrada no ZIP, nome seguro)`` — sem traversal, sempre com o ID."""
    safe_name = sanitize_filename(attachment.name)
    return f"{ATTACHMENT_ENTRY_PREFIX}/{attachment.id}-{safe_name}", safe_name


async def _fetch_attachment_bytes(attachment: Attachment, *, max_bytes: int) -> bytes:
    """Baixa o original com orçamento real; falha nunca omite o anexo."""
    try:
        body, _content_type = await storage_service.download_limited(
            attachment.storage_key,
            max_bytes=max_bytes,
            timeout_seconds=ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except StorageLimitExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - storage indisponível → 503 sem pacote
        logger.warning(
            "Failed to fetch patient attachment for record export", exc_info=True
        )
        raise _DossierStorageUnavailable(str(exc)) from exc
    return body


async def _build_patient_record_zip(
    *,
    patient: Patient,
    pdf: bytes,
    records: DossierRecords,
    export_id: UUID,
    purpose: str,
    generated_at: datetime,
) -> bytes:
    buffer = io.BytesIO()
    manifest_files: list[dict] = []
    total_bytes = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DOSSIER_PDF_ENTRY, pdf)
        for attachment in records.attachments:
            remaining = MAX_EXPORT_ATTACHMENT_BYTES - total_bytes
            if remaining <= 0:
                raise _ExportLimitExceeded(
                    error_code=ERROR_CODE_ATTACHMENT_LIMIT,
                    detail=_attachment_bytes_detail(),
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
            body = await _fetch_attachment_bytes(attachment, max_bytes=remaining)
            total_bytes += len(body)
            if total_bytes > MAX_EXPORT_ATTACHMENT_BYTES:
                raise _ExportLimitExceeded(
                    error_code=ERROR_CODE_ATTACHMENT_LIMIT,
                    detail=_attachment_bytes_detail(),
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
            entry, safe_name = attachment_entry_name(attachment)
            archive.writestr(entry, body)
            manifest_files.append(
                {
                    "attachmentId": str(attachment.id),
                    "entry": entry,
                    "name": safe_name,
                    "sizeBytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )

        manifest = {
            "manifestVersion": DOSSIER_MANIFEST_VERSION,
            "exportId": str(export_id),
            "kind": EXPORT_KIND_DOSSIER,
            "format": EXPORT_FORMAT_ZIP,
            "patientId": str(patient.id),
            "sections": list(records.sections),
            "purpose": purpose,
            "generatedAt": generated_at.isoformat(),
            "from": records.from_date.isoformat() if records.from_date else None,
            "to": records.to_date.isoformat() if records.to_date else None,
            "counts": records.counts,
            "document": {
                "entry": DOSSIER_PDF_ENTRY,
                "sizeBytes": len(pdf),
                "sha256": hashlib.sha256(pdf).hexdigest(),
            },
            "attachments": manifest_files,
            "notice": MANIFEST_NOTICE,
        }
        archive.writestr(
            DOSSIER_MANIFEST_ENTRY,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Dossiê selecionável — render fora do event loop e fluxo completo
# --------------------------------------------------------------------------- #

_render_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _render_semaphore() -> asyncio.Semaphore:
    """Semáforo por event loop (limita renderizações concorrentes)."""
    loop = asyncio.get_running_loop()
    semaphore = _render_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(RENDER_CONCURRENCY)
        _render_semaphores[loop] = semaphore
    return semaphore


async def _render_dossier_off_loop(**kwargs) -> bytes:
    async with _render_semaphore():
        return await asyncio.wait_for(
            asyncio.to_thread(render_patient_record_pdf, **kwargs),
            timeout=RENDER_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True)
class PatientRecordExportFile:
    export_id: UUID
    payload: bytes
    media_type: str
    filename: str


async def generate_patient_record_export(
    db: AsyncSession,
    *,
    patient: Patient,
    professional: Professional,
    request: PatientRecordExportRequest,
) -> PatientRecordExportFile:
    """Dossiê PDF/ZIP ponta a ponta: seleção, auditoria, render e resultado."""
    selected_attachments = await _load_selected_attachments(
        db, patient_id=patient.id, attachment_ids=request.attachment_ids
    )
    requested_at = datetime.now(UTC)
    try:
        export_id = await begin_patient_record_export(
            patient_id=patient.id,
            professional_id=professional.id,
            kind=EXPORT_KIND_DOSSIER,
            export_format=request.format,
            sections=list(request.sections),
            purpose=request.purpose,
            from_date=request.from_date,
            to_date=request.to_date,
            attachment_count=len(selected_attachments),
            requested_at=requested_at,
        )
    except Exception as exc:
        logger.exception("Failed to persist patient record export audit request")
        raise _unavailable(AUDIT_UNAVAILABLE_DETAIL) from exc

    try:
        async with asyncio.timeout(EXPORT_BUDGET_SECONDS):
            records = await collect_patient_record_export(
                db, patient=patient, request=request, attachments=selected_attachments
            )
            _assert_dossier_limits(records)
            identity = await build_document_identity(professional)
            pdf = await _render_dossier_off_loop(
                patient=patient,
                records=records,
                export_id=export_id,
                purpose=request.purpose,
                specialty_key=professional.specialty_key,
                identity=identity,
                generated_at=requested_at,
            )
            if request.format == EXPORT_FORMAT_ZIP:
                payload = await _build_patient_record_zip(
                    patient=patient,
                    pdf=pdf,
                    records=records,
                    export_id=export_id,
                    purpose=request.purpose,
                    generated_at=requested_at,
                )
            else:
                payload = pdf
    except _ExportLimitExceeded as exc:
        await _finalize_failure(export_id, error_code=exc.error_code)
        raise exc.as_http_exception() from None
    except _DossierStorageUnavailable as exc:
        await _finalize_failure(export_id, error_code=ERROR_CODE_STORAGE_UNAVAILABLE)
        raise _unavailable(DOSSIER_STORAGE_DETAIL) from exc
    except StorageLimitExceededError as exc:
        await _finalize_failure(export_id, error_code=ERROR_CODE_ATTACHMENT_LIMIT)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=_attachment_bytes_detail(),
        ) from exc
    except TimeoutError as exc:
        # Inclui o orçamento total da operação e o timeout do render.
        await _finalize_failure(export_id, error_code=ERROR_CODE_EXPORT_TIMEOUT)
        raise _unavailable(DOSSIER_TIMEOUT_DETAIL) from exc
    except HTTPException as exc:
        await _finalize_failure(export_id, error_code=ERROR_CODE_REQUEST_REJECTED)
        raise
    except Exception as exc:
        logger.exception("Failed to render patient record export")
        await _finalize_failure(export_id, error_code=ERROR_CODE_RENDER_FAILED)
        raise _unavailable(DOSSIER_FAILED_DETAIL) from exc

    try:
        await finalize_patient_record_export(
            export_id,
            result_status=EXPORT_STATUS_GENERATED,
            record_counts=records.counts,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    except Exception as exc:
        logger.exception("Failed to persist patient record export audit result")
        raise _unavailable(AUDIT_UNAVAILABLE_DETAIL) from exc

    return PatientRecordExportFile(
        export_id=export_id,
        payload=payload,
        media_type=MEDIA_TYPES[request.format],
        filename=f"prontuario-{export_id}.{request.format}",
    )


async def list_patient_record_exports(
    db: AsyncSession, *, patient_id: UUID, page: int, limit: int
) -> tuple[list[PatientRecordExport], int]:
    """Histórico de exportações do paciente (sem bytes, URLs ou conteúdo)."""
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(PatientRecordExport)
            .where(PatientRecordExport.patient_id == patient_id)
        )
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(PatientRecordExport)
                .where(PatientRecordExport.patient_id == patient_id)
                .order_by(
                    PatientRecordExport.requested_at.desc(),
                    PatientRecordExport.id.desc(),
                )
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).scalars()
    )
    return rows, total
