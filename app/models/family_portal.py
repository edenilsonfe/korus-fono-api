"""F14 — portal da família: acesso versionado, destinatários, eventos e grants.

Somente as tabelas M1 da onda 1 (acesso/grants). A camada editorial (itens,
revisões e públicos) chega na M2 e NÃO mora aqui.

Regras estruturais preservadas no banco:
- um portal por paciente;
- um destinatário por par portal/caregiver, com autorização append-only
  versionada (um evento por versão de autorização);
- caregiver desvinculado exige destinatário inativo (CHECK);
- um grant não revogado por destinatário (índice parcial PG/SQLite);
- versões de acesso do dono e do portal congeladas na emissão;
- eventos são trilha append-only: nunca guardam token, URL ou texto clínico.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_uuid


class FamilyPortal(Base, TimestampMixin):
    """Portal único por paciente; ausência de linha equivale a desabilitado."""

    __tablename__ = "family_portals"
    __table_args__ = (
        CheckConstraint(
            "access_version >= 0", name="ck_family_portals_access_version"
        ),
        CheckConstraint("version >= 1", name="ck_family_portals_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, unique=True
    )
    # Dono fixado na criação: transferência de paciente não transfere o portal.
    owner_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Epoch do portal: incrementado em desativação; grants capturam na emissão.
    access_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class FamilyPortalRecipient(Base, TimestampMixin):
    """Associação explícita portal/caregiver; nenhuma cópia de contato/nome."""

    __tablename__ = "family_portal_recipients"
    __table_args__ = (
        UniqueConstraint(
            "portal_id", "caregiver_id", name="uq_family_portal_recipient"
        ),
        CheckConstraint(
            "authorization_version >= 0",
            name="ck_family_portal_recipient_authorization_version",
        ),
        CheckConstraint(
            "version >= 1", name="ck_family_portal_recipient_version"
        ),
        # Caregiver removido (SET NULL) nunca fica autorizado: hook desativa antes.
        CheckConstraint(
            "caregiver_id IS NOT NULL OR active = false",
            name="ck_family_portal_recipient_link",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portals.id"),
        nullable=False,
        index=True,
    )
    caregiver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("caregivers.id", ondelete="SET NULL"),
        nullable=True,
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    appointments_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Sequência append-only da autorização; a autorização vigente é o evento
    # desta versão, não "o último concedido" (uma retirada posterior vale).
    authorization_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class FamilyPortalEvent(Base):
    """Trilha append-only das decisões profissionais do portal.

    Nunca guarda token, URL, IP bruto ou texto clínico. ``grant_id``/``item_id``
    são identificadores de auditoria (sem FK circular) e nenhum lookup público
    usa esses campos para autorizar. Para eventos de autorização o payload tem
    apenas data/reference/reviewed/reason.
    """

    __tablename__ = "family_portal_events"
    __table_args__ = (
        UniqueConstraint(
            "recipient_id",
            "authorization_version",
            name="uq_family_portal_event_authorization",
        ),
        CheckConstraint(
            "authorization_version IS NULL OR authorization_version >= 1",
            name="ck_family_portal_event_authorization_version",
        ),
        Index(
            "ix_family_portal_events_portal_timeline",
            "portal_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_portals.id"), nullable=False
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portal_recipients.id"),
        nullable=True,
    )
    actor_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class FamilyPortalGrant(Base, TimestampMixin):
    """Link restrito da família: token opaco com apenas o hash persistido.

    Um grant por par paciente/responsável não revogado (índice parcial). A
    rotação revoga o anterior na MESMA transação; o token bruto só existe na
    resposta de emissão e nunca é gravado, indexado ou logado.
    """

    __tablename__ = "family_portal_grants"
    __table_args__ = (
        CheckConstraint(
            "owner_access_version >= 0",
            name="ck_family_portal_grants_owner_access_version",
        ),
        CheckConstraint(
            "portal_access_version >= 0",
            name="ck_family_portal_grants_portal_access_version",
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_family_portal_grants_expiry"
        ),
        Index(
            "uq_family_portal_grants_active",
            "recipient_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_family_portal_grants_recipient_created",
            "recipient_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portal_recipients.id"),
        nullable=False,
    )
    # Evento de autorização exato usado na emissão (nunca "o último grant").
    authorization_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_portal_events.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    owner_access_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    portal_access_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
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
