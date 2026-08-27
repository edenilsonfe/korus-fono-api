import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, new_uuid


class Professional(Base, TimestampMixin):
    __tablename__ = "professionals"
    __table_args__ = (
        CheckConstraint(
            "admin_role IS NULL OR admin_role IN ('support', 'billing', 'product', 'superadmin')",
            name="ck_professionals_admin_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialty: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    specialty_key: Mapped[str] = mapped_column(String(32), default="fono", nullable=False)
    council: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    cpf: Mapped[str] = mapped_column(String(14), default="", nullable=False)
    billing_cnpj: Mapped[str] = mapped_column(String(14), default="", nullable=False)
    billing_document_type: Mapped[str] = mapped_column(
        String(4), default="cpf", server_default="cpf", nullable=False
    )
    billing_address: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    billing_address_number: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    billing_address_complement: Mapped[str] = mapped_column(
        String(100), default="", nullable=False
    )
    billing_province: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    billing_postal_code: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(64), default="oklch(0.58 0.12 205)", nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    admin_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    is_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    subscription_status: Mapped[str] = mapped_column(String(32), nullable=False, default="trialing")
    signup_payment_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    onboarding_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_dismissed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_viewed_demo_patient_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    onboarding_viewed_demo_result_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    patients: Mapped[list["Patient"]] = relationship(back_populates="professional")  # noqa: F821
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="professional")  # noqa: F821
