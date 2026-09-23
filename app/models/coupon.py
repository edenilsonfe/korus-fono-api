import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, new_uuid


class Coupon(Base, TimestampMixin):
    __tablename__ = "coupons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    coupon_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trial_bonus_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_per_professional: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_slugs: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    external_coupon_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CouponRedemption(Base, TimestampMixin):
    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'confirmed', 'released', 'refunded')",
            name="ck_coupon_redemptions_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context: Mapped[str] = mapped_column(String(32), nullable=False, default="checkout")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    checkout_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    plan_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discounted_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
