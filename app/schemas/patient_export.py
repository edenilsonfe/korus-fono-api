"""F6 — DTOs das exportações do prontuário (resumo B e dossiê selecionável).

Somente o resumo B (``GET /patients/{id}/export.pdf``) está implementado nesta
tarefa; os payloads do dossiê (``POST /patients/{id}/record-exports``) ficam
congelados aqui para a fase seguinte.
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.schemas.common import CamelModel

PatientExportSection = Literal[
    "identification",
    "anamnesis",
    "assessments",
    "evolutions",
    "goals",
    "sessions",
    "attachments",
]
PatientRecordExportKind = Literal["summary", "dossier"]
PatientRecordExportFormat = Literal["pdf", "zip"]
PatientRecordExportStatus = Literal["requested", "generated", "failed"]
PatientRecordExportPurpose = Literal[
    "care_continuity", "patient_request", "professional_archive"
]


class PatientSummaryExportQuery(CamelModel):
    """Query do resumo B: apenas o limite de sessões é parametrizável.

    O conteúdo é fixo por decisão de produto — nenhum parâmetro adiciona
    anamnese, evoluções ou anexos ao documento.
    """

    sessions_limit: int = Field(default=10, ge=1, le=50)


class PatientRecordExportRequest(CamelModel):
    """Payload do dossiê selecionável (implementação completa na fase 2)."""

    model_config = ConfigDict(extra="forbid")

    format: PatientRecordExportFormat
    sections: list[PatientExportSection]
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    attachment_ids: list[UUID] = Field(default_factory=list)
    purpose: PatientRecordExportPurpose
    confirm_sensitive_content: bool

    @model_validator(mode="after")
    def _validate_selection(self) -> "PatientRecordExportRequest":
        if "identification" not in self.sections:
            raise ValueError("A seção identification é obrigatória.")
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections não pode conter duplicatas.")
        if (
            self.from_date is not None
            and self.to_date is not None
            and self.from_date > self.to_date
        ):
            raise ValueError("from não pode ser posterior a to.")
        if self.attachment_ids and "attachments" not in self.sections:
            raise ValueError("attachmentIds exige a seção attachments.")
        return self


class PatientRecordExportResponse(CamelModel):
    """Item de auditoria exposto ao dono — sem bytes, URLs ou conteúdo clínico."""

    id: str
    kind: PatientRecordExportKind
    format: PatientRecordExportFormat
    sections: list[str]
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    purpose: str
    status: PatientRecordExportStatus
    requested_at: datetime
    completed_at: datetime | None = None
    actor_professional_id: str
    record_counts: dict | None = None
    attachment_count: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error_code: str | None = None
