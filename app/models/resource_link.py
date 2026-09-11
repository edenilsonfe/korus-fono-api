"""F17 — vínculos de recursos com domínios clínicos, metas e programas ABA.

``ResourceDomainLink`` usa chaves de ``CLINICAL_DOMAIN_CATALOG`` (nunca IDs de
snapshot); ``GoalResourceLink``/``ProgramResourceLink`` usam FKs reais. Toda
tabela tem UNIQUE por par para que PUT/replay idempotente não duplique linhas.
O vínculo registra autoria e nunca altera meta, score ou publicação.
"""

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class ResourceDomainLink(Base, TimestampMixin):
    __tablename__ = "resource_domain_links"
    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "domain_key",
            name="uq_resource_domain_links_resource_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class GoalResourceLink(Base, TimestampMixin):
    __tablename__ = "goal_resource_links"
    __table_args__ = (
        UniqueConstraint("goal_id", "resource_id", name="uq_goal_resource_links_goal_resource"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class ProgramResourceLink(Base, TimestampMixin):
    __tablename__ = "program_resource_links"
    __table_args__ = (
        UniqueConstraint(
            "program_id", "resource_id", name="uq_program_resource_links_program_resource"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intervention_programs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
