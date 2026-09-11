"""F17/4.3 — fila pequena de limpeza de blobs (``StorageCleanupTask``).

Reserva registrada ANTES do upload (committada, para sobreviver a queda do
processo) e retirada na transação que associa o blob; o janitor
(``app.services.storage_cleanup_service``) só remove chaves sem referência
viva. A chave é sempre gerada pelo servidor (nunca vem do cliente) e
``last_error`` guarda diagnóstico sanitizado, sem a chave crua. Falha além do
limite de tentativas termina em ``failed`` — visível ao operador, nunca
descartada em silêncio.

Estados: ``pending`` (aguardando limpeza), ``resolved`` (blob associado, nada a
fazer), ``deleted`` (objeto removido ou já ausente), ``kept`` (referência viva
encontrada; objeto preservado) e ``failed`` (tentativas esgotadas).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid

STORAGE_CLEANUP_PENDING = "pending"
STORAGE_CLEANUP_RESOLVED = "resolved"
STORAGE_CLEANUP_DELETED = "deleted"
STORAGE_CLEANUP_KEPT = "kept"
STORAGE_CLEANUP_FAILED = "failed"


class StorageCleanupTask(Base, TimestampMixin):
    __tablename__ = "storage_cleanup_tasks"
    __table_args__ = (
        Index(
            "ix_storage_cleanup_tasks_status_not_before",
            "status",
            "not_before",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STORAGE_CLEANUP_PENDING,
        server_default=STORAGE_CLEANUP_PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    # Diagnóstico sanitizado (sem a chave crua nem o bucket) — nunca entra
    # conteúdo sensível; é o que o operador vê quando algo falha além do limite.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Motivo operacional: resource_create | resource_replace |
    # resource_replace_previous | resource_delete (F16 acrescenta os dela).
    reason: Mapped[str] = mapped_column(String(32), nullable=False, default="resource")
    created_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
