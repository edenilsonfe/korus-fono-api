"""Real PostgreSQL transactions; use a disposable local cluster only."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.affiliate import (
    AffiliateLedgerEntry,
    AffiliateParticipant,
)
from app.models.professional import Professional
from app.services.affiliate_credit_service import (
    AffiliateCreditForbiddenError,
    AffiliateCreditService,
)
from app.services.affiliate_payout_service import (
    AffiliatePayoutForbiddenError,
    AffiliatePayoutService,
)
from app.services.affiliate_portal_service import (
    AffiliatePortalForbiddenError,
    AffiliatePortalService,
)
from app.services.affiliate_service import AffiliateService

pytestmark = pytest.mark.asyncio


async def test_legacy_credit_migration_preserves_ledger_and_rejects_ambiguous_history(
    pg_factory,
):
    import importlib.util
    from pathlib import Path

    from app.models.affiliate import AffiliateCreditCheckout
    from app.models.billing import Plan, Subscription

    spec = importlib.util.spec_from_file_location(
        "affiliate_migration",
        Path(__file__).parents[1]
        / "alembic/versions/766aed0ea62c_affiliate_financial_integrity.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    professional_id, participant_id = await seed(pg_factory)
    reservation_id = str(uuid4())
    async with pg_factory() as db:
        plan = Plan(
            slug="legacy", name="Legacy", price_cents=10000, billing_interval="monthly"
        )
        db.add(plan)
        await db.flush()
        db.add(
            Subscription(
                professional_id=professional_id,
                plan_id=plan.id,
                status="incomplete",
                checkout_session_id=reservation_id,
                checkout_charge_cents=5000,
                external_checkout_id="pay-legacy",
            )
        )
        for account, amount in [("credit", -5000), ("reserved", 5000)]:
            db.add(
                AffiliateLedgerEntry(
                    participant_id=participant_id,
                    account=account,
                    amount_cents=amount,
                    entry_type="credit_checkout_reserved",
                    idempotency_key=f"credit-reservation:{reservation_id}:{account}",
                )
            )
        await db.commit()
        connection = await db.connection()
        await connection.run_sync(migration.backfill_legacy_credit)
        row = await db.scalar(select(AffiliateCreditCheckout))
        assert (
            row.state == "reserved"
            and row.amount_cents == 5000
            and row.charge_cents == 10000
        )
        assert row.source_payment_id == "pay-legacy"
        assert (await AffiliateService(db).balances(participant_id))["reserved"] == 5000
        await db.rollback()
        for suffix, account, amount in [
            ("settled", "reserved", -5000),
            ("released-credit", "credit", 5000),
        ]:
            db.add(
                AffiliateLedgerEntry(
                    participant_id=participant_id,
                    account=account,
                    amount_cents=amount,
                    entry_type="legacy",
                    idempotency_key=f"credit-reservation:{reservation_id}:{suffix}",
                )
            )
        await db.flush()
        connection = await db.connection()
        with pytest.raises(RuntimeError, match="reconciliation"):
            await connection.run_sync(migration.backfill_legacy_credit)


@pytest.fixture
async def pg_factory(monkeypatch):
    url = os.getenv("TEST_AFFILIATE_PG_URL")
    if not url:
        pytest.skip("Set TEST_AFFILIATE_PG_URL to a disposable local PostgreSQL")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 55439, (
        "Only disposable audit cluster is allowed"
    )
    schema = "affiliate_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key", lambda: key
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def seed(factory):
    async with factory() as db:
        professional = Professional(
            email="race@example.com",
            password_hash="unused",
            name="Race",
            subscription_status="active",
            email_verified_at=datetime.now(UTC),
        )
        db.add(professional)
        await db.flush()
        participant = AffiliateParticipant(
            professional_id=professional.id,
            email=professional.email,
            customer_enabled=True,
            partner_enabled=True,
            status="active",
        )
        db.add(participant)
        await db.flush()
        profile = await AffiliatePayoutService(db).submit_fiscal_profile(
            participant=participant,
            person_type="pf",
            legal_name="Race",
            document="52998224725",
            pix_key_type="cpf",
            pix_key="52998224725",
        )
        profile.status = "approved"
        profile.pix_validated_at = datetime.now(UTC)
        profile.withdrawal_locked_until = datetime.now(UTC) - timedelta(days=1)
        db.add(
            AffiliateLedgerEntry(
                participant_id=participant.id,
                account="available",
                amount_cents=10000,
                entry_type="test_seed",
                idempotency_key="seed",
            )
        )
        await db.commit()
        return professional.id, participant.id


@pytest.mark.parametrize("second_operation", ["payout", "credit"])
async def test_simultaneous_resgates_cannot_spend_same_balance(
    pg_factory, second_operation
):
    professional_id, participant_id = await seed(pg_factory)
    barrier = asyncio.Barrier(2)

    async def spend(operation):
        async with pg_factory() as db:
            participant = await db.get(AffiliateParticipant, participant_id)
            professional = await db.get(Professional, professional_id)
            await barrier.wait()
            try:
                if operation == "payout":
                    await AffiliatePayoutService(db).request_cash_payout(
                        participant=participant,
                        amount_cents=10000,
                        cash_enabled=True,
                        request_id=str(uuid4()),
                    )
                else:
                    await AffiliateCreditService(db).convert_available_to_credit(
                        professional=professional,
                        amount_cents=10000,
                        idempotency_key="race-credit",
                    )
                await db.commit()
                return "spent"
            except (AffiliatePayoutForbiddenError, AffiliateCreditForbiddenError):
                await db.rollback()
                return "blocked"

    assert sorted(
        await asyncio.wait_for(
            asyncio.gather(spend("payout"), spend(second_operation)), 10
        )
    ) == ["blocked", "spent"]
    async with pg_factory() as db:
        balances = await AffiliateService(db).balances(participant_id)
        assert balances["available"] == 0
        assert balances["reserved"] + balances["credit"] == 10000


async def test_magic_link_consumed_once_with_two_transactions(pg_factory):
    _, participant_id = await seed(pg_factory)
    async with pg_factory() as db:
        participant = await db.get(AffiliateParticipant, participant_id)
        token = await AffiliatePortalService(db).create_magic_link(participant)
        await db.commit()
    barrier = asyncio.Barrier(2)

    async def exchange():
        async with pg_factory() as db:
            await barrier.wait()
            try:
                await AffiliatePortalService(db).exchange_magic_link(token)
                await db.commit()
                return "exchanged"
            except AffiliatePortalForbiddenError:
                await db.rollback()
                return "blocked"

    assert sorted(
        await asyncio.wait_for(asyncio.gather(exchange(), exchange()), 10)
    ) == ["blocked", "exchanged"]
