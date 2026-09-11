"""F17 — licenças de distribuição de recursos.

``ResourceLicense`` registra cada declaração de licença (versão por recurso)
e ``ResourceLicenseDecision`` registra as decisões de curadoria em modo
append-only. A licença fica ligada ao hash do conteúdo verificado no momento
da declaração; expiração é derivada de ``valid_until`` (nunca por job).
"""

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, new_uuid


class ResourceLicense(Base, TimestampMixin):
    __tablename__ = "resource_licenses"
    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "version",
            name="uq_resource_licenses_resource_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # declared | pending | approved | rejected | revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # original | licensed | public_domain
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    rights_holder: Mapped[str] = mapped_column(String(255), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    evidence_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attribution: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    allow_professional_distribution: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    allow_family_delivery: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Hash do arquivo a que esta licença se aplica (verificado no service).
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    declared_by_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # true quando a declaração foi registrada pela curadoria (material global).
    declared_by_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ResourceLicenseDecision(Base, TimestampMixin):
    __tablename__ = "resource_license_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    license_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resource_licenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # approved | rejected | revoked
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
