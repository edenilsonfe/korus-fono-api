"""F14 onda 2 — camada editorial: itens, revisões imutáveis e público vigente.

Somente as tabelas M2 (conteúdo publicado e suas revisões). O acesso/grants
moram em ``app/models/family_portal.py`` (M1). Regras estruturais preservadas
no banco:

- ``kind``/``status`` fechados; a fonte permitida depende do tipo (CHECK por
  tipo, sem XOR frouxo que aceite dois domínios);
- rascunho (``draft_content``/``draft_recipient_ids``) é separado da revisão
  publicada; ``published_version`` aponta para a revisão exata do par
  (item, versão) e nunca para o rascunho;
- revisões são append-only: retirada/edição não reescreve o JSON aprovado;
- o público vigente é um conjunto de pares únicos na tabela de audiências —
  nunca a lista histórica gravada na revisão;
- FKs de fonte não usam CASCADE destrutivo: histórico bloqueia exclusão.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_uuid

ITEM_KINDS = ("session_summary", "goal", "notice", "material", "report")
ITEM_STATUSES = ("draft", "published", "withdrawn")

# Cada tipo admite exatamente uma fonte: session_summary usa session_id (com
# evolution_id opcional); goal usa só goal_id; notice não tem fonte; material
# usa só resource_id; report usa só delivery_id. NULL não vaza para outro tipo.
_ITEM_SOURCE_CHECK = (
    "(kind = 'session_summary' AND session_id IS NOT NULL"
    " AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NULL)"
    " OR (kind = 'goal' AND session_id IS NULL AND evolution_id IS NULL"
    " AND goal_id IS NOT NULL AND resource_id IS NULL AND delivery_id IS NULL)"
    " OR (kind = 'notice' AND session_id IS NULL AND evolution_id IS NULL"
    " AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NULL)"
    " OR (kind = 'material' AND session_id IS NULL AND evolution_id IS NULL"
    " AND goal_id IS NULL AND resource_id IS NOT NULL AND delivery_id IS NULL)"
    " OR (kind = 'report' AND session_id IS NULL AND evolution_id IS NULL"
    " AND goal_id IS NULL AND resource_id IS NULL AND delivery_id IS NOT NULL)"
)


class FamilyPortalItem(Base, TimestampMixin):
    """Item editorial: rascunho + ponteiro para a revisão publicada vigente.

    O item NÃO guarda FK circular para a revisão; a publicação insere a
    revisão e atualiza ``published_version`` na MESMA transação sob lock. Um
    ponteiro órfão falha fechado na leitura pública (nunca cai no rascunho).
    """

    __tablename__ = "family_portal_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('session_summary', 'goal', 'notice', 'material', 'report')",
            name="ck_family_portal_items_kind",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'withdrawn')",
            name="ck_family_portal_items_status",
        ),
        CheckConstraint("version >= 1", name="ck_family_portal_items_version"),
        CheckConstraint(
            "published_version IS NULL OR published_version >= 1",
            name="ck_family_portal_items_published_version",
        ),
        CheckConstraint(
            "published_version IS NULL OR published_version <= version",
            name="ck_family_portal_items_published_not_future",
        ),
        CheckConstraint(
            "status <> 'published' OR published_version IS NOT NULL",
            name="ck_family_portal_items_published_pointer",
        ),
        CheckConstraint(_ITEM_SOURCE_CHECK, name="ck_family_portal_items_source"),
        Index(
            "ix_family_portal_items_portal_kind_status",
            "portal_id",
            "kind",
            "status",
            "published_at",
            "id",
        ),
        Index("ix_family_portal_items_session", "session_id"),
        Index("ix_family_portal_items_evolution", "evolution_id"),
        Index("ix_family_portal_items_goal", "goal_id"),
        Index("ix_family_portal_items_resource", "resource_id"),
        Index("ix_family_portal_items_delivery", "delivery_id"),
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
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Ponteiro para a revisão publicada vigente; nunca para o rascunho.
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Rascunho editorial tipado pelo servidor (nunca JSON livre do cliente).
    draft_content: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    draft_recipient_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    # Fingerprint privado da fonte na última validação do rascunho.
    draft_source_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Fontes por tipo (CHECK acima garante exatamente uma quando aplicável).
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    evolution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evolutions.id"), nullable=True
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goals.id"), nullable=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id"), nullable=True
    )
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_deliveries.id"), nullable=True
    )
    created_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FamilyPortalItemRevision(Base):
    """Revisão imutável publicada (append-only); texto aprovado apenas.

    Nunca copia evolução/notes/respostas brutas: o conteúdo é o texto
    editorial revisado; ``source_metadata`` carrega apenas IDs/datas/versões
    produzidos pelo servidor. Para avisos, ``expires_at`` é o vencimento
    calculado na publicação (vence por leitura, sem cron).
    """

    __tablename__ = "family_portal_item_revisions"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "version", name="uq_family_portal_item_revision"
        ),
        CheckConstraint(
            "version >= 1", name="ck_family_portal_item_revision_version"
        ),
        Index(
            "ix_family_portal_item_revisions_item_published",
            "item_id",
            "published_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portal_items.id"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    recipient_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    source_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    source_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    published_by_professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FamilyPortalItemAudience(Base):
    """Público vigente do item: pares únicos (item, destinatário).

    Publicar substitui o conjunto atomicamente; retirar remove sem apagar a
    revisão. A lista histórica da revisão é evidência, nunca ACL atual.
    """

    __tablename__ = "family_portal_item_audiences"
    __table_args__ = (
        Index(
            "ix_family_portal_item_audiences_recipient",
            "recipient_id",
            "item_id",
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portal_items.id"),
        primary_key=True,
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("family_portal_recipients.id"),
        primary_key=True,
    )
