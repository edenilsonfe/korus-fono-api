"""affiliate_financial_integrity

Revision ID: 766aed0ea62c
Revises: z7a8b9c0d1e2
Create Date: 2026-09-05 00:16:13.815250

"""

from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "766aed0ea62c"
down_revision: Union[str, Sequence[str], None] = "z7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def backfill_legacy_credit(bind) -> None:
    """Adopt only unambiguous old reservations; never rewrite their ledger."""
    entries = (
        bind.execute(
            sa.text("""SELECT participant_id, amount_cents, metadata,
        idempotency_key FROM affiliate_ledger_entries
        WHERE account = 'reserved' AND entry_type = 'credit_checkout_reserved'
        AND idempotency_key LIKE :pattern"""),
            {"pattern": "credit-reservation:%:reserved"},
        )
        .mappings()
        .all()
    )
    for entry in entries:
        reservation_id = entry["idempotency_key"][
            len("credit-reservation:") : -len(":reserved")
        ]
        prefix = f"credit-reservation:{reservation_id}:"
        closed = dict(
            bind.execute(
                sa.text("""SELECT idempotency_key, amount_cents
            FROM affiliate_ledger_entries WHERE idempotency_key IN (:credit, :released, :settled)"""),
                {
                    "credit": prefix + "credit",
                    "released": prefix + "released-credit",
                    "settled": prefix + "settled",
                },
            ).all()
        )
        amount = entry["amount_cents"]
        released = prefix + "released-credit" in closed
        settled = prefix + "settled" in closed
        if (
            amount <= 0
            or closed.get(prefix + "credit") != -amount
            or (released and settled)
        ):
            raise RuntimeError(
                f"Legacy affiliate reservation {reservation_id} requires ledger reconciliation"
            )
        sub = (
            bind.execute(
                sa.text("""SELECT s.external_checkout_id, s.checkout_charge_cents
            FROM subscriptions s JOIN affiliate_participants p ON p.professional_id = s.professional_id
            WHERE p.id = :participant AND CAST(s.checkout_session_id AS text) = :reservation"""),
                {"participant": entry["participant_id"], "reservation": reservation_id},
            )
            .mappings()
            .first()
        )
        if not sub and not released:
            raise RuntimeError(
                f"Legacy affiliate reservation {reservation_id} has no traceable subscription"
            )
        state = "released" if released else "settled" if settled else "reserved"
        bind.execute(
            sa.text("""INSERT INTO affiliate_credit_checkouts
            (id, participant_id, reservation_id, attempt, amount_cents, charge_cents, refunded_cents, state, source_payment_id)
            VALUES (:id, :participant, :reservation, 1, :amount, :charge, 0, :state, :payment)"""),
            {
                "id": uuid4(),
                "participant": entry["participant_id"],
                "reservation": reservation_id,
                "amount": amount,
                "charge": amount + int(sub["checkout_charge_cents"] or 0)
                if sub
                else amount,
                "state": state,
                "payment": sub["external_checkout_id"]
                if sub and not released
                else None,
            },
        )


def upgrade() -> None:
    """Upgrade schema."""
    # Reviewed: only affiliate changes; existing index/FK drift is intentionally excluded.
    duplicate = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS (SELECT 1 FROM affiliate_payout_requests
        WHERE provider_transfer_id IS NOT NULL GROUP BY provider_transfer_id HAVING count(*) > 1)
    """)
        )
        .scalar()
    )
    if duplicate:
        raise RuntimeError(
            "Duplicate affiliate transfer ids require reconciliation before migration"
        )
    op.create_table(
        "affiliate_credit_checkouts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("participant_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("charge_cents", sa.Integer(), nullable=False),
        sa.Column("refunded_cents", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source_payment_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'released', 'settled', 'refunded')",
            name="ck_affiliate_credit_checkout_state",
        ),
        sa.CheckConstraint(
            "amount_cents >= 0 AND refunded_cents >= 0 AND refunded_cents <= amount_cents",
            name="ck_affiliate_credit_checkout_amount",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["affiliate_participants.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reservation_id", "attempt", name="uq_affiliate_credit_checkout_attempt"
        ),
        sa.UniqueConstraint("source_payment_id"),
    )
    op.create_index(
        op.f("ix_affiliate_credit_checkouts_participant_id"),
        "affiliate_credit_checkouts",
        ["participant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_affiliate_credit_checkouts_reservation_id"),
        "affiliate_credit_checkouts",
        ["reservation_id"],
        unique=False,
    )
    op.add_column(
        "affiliate_payout_requests",
        sa.Column("request_id", sa.String(length=64), nullable=True),
    )
    op.drop_index(
        op.f("ix_affiliate_payout_requests_provider_transfer_id"),
        table_name="affiliate_payout_requests",
    )
    op.create_index(
        op.f("ix_affiliate_payout_requests_provider_transfer_id"),
        "affiliate_payout_requests",
        ["provider_transfer_id"],
        unique=True,
    )
    op.create_unique_constraint(
        "uq_affiliate_payout_request_id",
        "affiliate_payout_requests",
        ["participant_id", "request_id"],
    )
    op.add_column(
        "subscriptions",
        sa.Column("checkout_recurring_price_cents", sa.Integer(), nullable=True),
    )
    op.add_column(
        "billing_events",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    backfill_legacy_credit(op.get_bind())


def downgrade() -> None:
    """Downgrade schema."""
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM affiliate_credit_checkouts)"))
        .scalar()
    ):
        raise RuntimeError(
            "Cannot downgrade affiliate accounting with reservation history"
        )
    op.drop_constraint(
        "uq_affiliate_payout_request_id", "affiliate_payout_requests", type_="unique"
    )
    op.drop_index(
        "ix_affiliate_payout_requests_provider_transfer_id",
        table_name="affiliate_payout_requests",
    )
    op.create_index(
        "ix_affiliate_payout_requests_provider_transfer_id",
        "affiliate_payout_requests",
        ["provider_transfer_id"],
        unique=False,
    )
    op.drop_column("affiliate_payout_requests", "request_id")
    op.drop_column("subscriptions", "checkout_recurring_price_cents")
    op.drop_column("billing_events", "last_attempt_at")
    op.drop_table("affiliate_credit_checkouts")
