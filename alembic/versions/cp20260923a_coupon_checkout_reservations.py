"""Track coupon reservations against the payment that uses them."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "cp20260923a"
down_revision = "cw20260922a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "coupon_redemptions",
        sa.Column("state", sa.String(length=16), nullable=False, server_default="confirmed"),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("checkout_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("plan_slug", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("discounted_price_cents", sa.Integer(), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("external_payment_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "coupon_redemptions",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_coupon_redemptions_subscription_id",
        "coupon_redemptions",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_coupon_redemptions_state",
        "coupon_redemptions",
        "state IN ('reserved', 'confirmed', 'released', 'refunded')",
    )
    for column in ("subscription_id", "checkout_session_id", "external_payment_id"):
        op.create_index(f"ix_coupon_redemptions_{column}", "coupon_redemptions", [column])


def downgrade() -> None:
    for column in ("external_payment_id", "checkout_session_id", "subscription_id"):
        op.drop_index(f"ix_coupon_redemptions_{column}", table_name="coupon_redemptions")
    op.drop_constraint("ck_coupon_redemptions_state", "coupon_redemptions", type_="check")
    op.drop_constraint("fk_coupon_redemptions_subscription_id", "coupon_redemptions", type_="foreignkey")
    for column in (
        "refunded_at", "released_at", "reserved_until", "external_payment_id",
        "discounted_price_cents", "plan_slug", "checkout_session_id", "subscription_id", "state",
    ):
        op.drop_column("coupon_redemptions", column)
