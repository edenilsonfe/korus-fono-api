import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid

# Resumo individual (design B do spike 017) versus dossiê PDF/ZIP selecionável.
EXPORT_KIND_SUMMARY = "summary"
EXPORT_KIND_DOSSIER = "dossier"
EXPORT_KINDS = frozenset({EXPORT_KIND_SUMMARY, EXPORT_KIND_DOSSIER})

EXPORT_FORMAT_PDF = "pdf"
EXPORT_FORMAT_ZIP = "zip"
EXPORT_FORMATS = frozenset({EXPORT_FORMAT_PDF, EXPORT_FORMAT_ZIP})

# `requested` é a tentativa autorizada, persistida antes da geração.
# `generated` significa gerado/disponibilizado (não confirma download).
EXPORT_STATUS_REQUESTED = "requested"
EXPORT_STATUS_GENERATED = "generated"
EXPORT_STATUS_FAILED = "failed"
EXPORT_STATUSES = frozenset(
    {EXPORT_STATUS_REQUESTED, EXPORT_STATUS_GENERATED, EXPORT_STATUS_FAILED}
)

EXPORT_PURPOSE_CARE_CONTINUITY = "care_continuity"
EXPORT_PURPOSE_PATIENT_REQUEST = "patient_request"
EXPORT_PURPOSE_PROFESSIONAL_ARCHIVE = "professional_archive"
EXPORT_PURPOSES = frozenset(
    {
        EXPORT_PURPOSE_CARE_CONTINUITY,
        EXPORT_PURPOSE_PATIENT_REQUEST,
        EXPORT_PURPOSE_PROFESSIONAL_ARCHIVE,
    }
)


class PatientRecordExport(Base, TimestampMixin):
    """Auditoria de exportação do prontuário (F6).

    Registro próprio — não é evento de timeline clínica. Guarda a tentativa
    autorizada (``requested``) e o resultado (``generated``/``failed``), além
    de seleção, contagem e hash do arquivo. Nunca armazena o documento, bytes,
    URLs de storage ou conteúdo clínico (apenas contagens/motivo de erro).
    """

    __tablename__ = "patient_record_exports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    sections: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EXPORT_STATUS_REQUESTED
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    record_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attachment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
