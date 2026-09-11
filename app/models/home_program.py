"""F16 — programa de casa: prescrição doméstica, grants e registros.

Tabelas completas do contrato da F16 (M5). A prescrição (draft → active →
archived) e os grants com token rotativo são operados pela Tarefa 5.1; as
respostas familiares (check-ins, revisões) e as fotos são operadas pelas tarefas
5.2/5.3 sobre estas mesmas tabelas.

Regras estruturais preservadas no banco:
- XOR de alvo por tarefa (meta comum ou programa ABA);
- um client task id exclusivo por programa;
- uma resposta atual por tarefa;
- um grant não revogado por programa (índice parcial);
- uma foto vigente por resposta (índice parcial);
- eventos de comando idempotentes por (grant, client record id).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_uuid


class HomeProgram(Base, TimestampMixin):
    __tablename__ = "home_programs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_home_program_status",
        ),
        CheckConstraint("version >= 1", name="ck_home_program_version"),
        CheckConstraint("ends_on >= starts_on", name="ck_home_program_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True
    )
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/Sao_Paulo"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )


class HomeProgramTask(Base, TimestampMixin):
    __tablename__ = "home_program_tasks"
    __table_args__ = (
        UniqueConstraint(
            "program_id", "client_task_id", name="uq_home_program_task_client_id"
        ),
        UniqueConstraint(
            "program_id", "position", name="uq_home_program_task_position"
        ),
        CheckConstraint(
            "(goal_id IS NOT NULL) <> (intervention_program_id IS NOT NULL)",
            name="ck_home_program_task_target_xor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Ordem de exibição definida na prescrição.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Identificador do cliente (wire ``id``), exclusivo dentro do programa.
    client_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id"), nullable=True
    )
    intervention_program_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intervention_programs.id"),
        nullable=True,
    )


class HomeProgramTaskResource(Base, TimestampMixin):
    """Material familiar fixado na tarefa (recurso/licença/checksum)."""

    __tablename__ = "home_program_task_resources"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "resource_id", name="uq_home_program_task_resource"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id"), nullable=False, index=True
    )
    title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    license_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resource_licenses.id", ondelete="SET NULL"),
        nullable=True,
    )
    license_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class HomeProgramGrant(Base, TimestampMixin):
    """Link restrito da família: token opaco com hash e rotação por programa.

    ``caregiver_id`` usa SET NULL com snapshot de autoria: excluir o responsável
    revoga o grant antes de removê-lo, sem apagar a referência histórica.
    """

    __tablename__ = "home_program_grants"
    __table_args__ = (
        Index(
            "uq_home_program_grants_active",
            "program_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    caregiver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("caregivers.id", ondelete="SET NULL"),
        nullable=True,
    )
    caregiver_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    caregiver_relation_snapshot: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=True
    )
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    # Evento de consentimento (equipe assistencial) usado nesta concessão.
    consent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_sharing_consent_events.id"),
        nullable=True,
    )
    # Declaração específica da família para o programa de casa (não substitui
    # o consentimento da equipe assistencial nem autoriza outros usos).
    family_authorization: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class HomeProgramCheckIn(Base):
    """Resposta familiar atual da tarefa (uma por tarefa)."""

    __tablename__ = "home_program_check_ins"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_home_program_check_in_task"),
        CheckConstraint("version >= 1", name="ck_home_program_check_in_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    done: Mapped[bool] = mapped_column(Boolean, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HomeProgramCheckInRevision(Base):
    """Conteúdo anterior de uma resposta, com o grant que a registrou."""

    __tablename__ = "home_program_check_in_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    check_in_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_check_ins.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HomeProgramPhoto(Base, TimestampMixin):
    """Foto privada da resposta: pending → ready → deleted; nunca Recursos."""

    __tablename__ = "home_program_photos"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'deleted')",
            name="ck_home_program_photo_status",
        ),
        Index(
            "uq_home_program_photos_current",
            "check_in_id",
            unique=True,
            postgresql_where=text("status <> 'deleted'"),
            sqlite_where=text("status <> 'deleted'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    check_in_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_check_ins.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class HomeProgramEvent(Base):
    """Operação familiar idempotente: hash, IDs e versão resultante.

    Nunca guarda comentário ou token — apenas o comando e o resultado
    (``UNIQUE(grant_id, client_record_id)`` torna replays idempotentes).
    """

    __tablename__ = "home_program_events"
    __table_args__ = (
        UniqueConstraint(
            "grant_id",
            "client_record_id",
            name="uq_home_program_event_client_record",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("home_program_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    check_in_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
