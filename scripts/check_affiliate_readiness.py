"""Read-only affiliate release preflight. DATABASE_URL must be injected explicitly."""

import argparse
import asyncio
import json
import os

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def check(expect_cash: bool) -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit(
            "Injete DATABASE_URL explicitamente; o script não carrega .env"
        )
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Este preflight exige PostgreSQL")
    engine = create_async_engine(url)
    report = {}
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            queries = {
                "duplicate_transfer_ids": "SELECT count(*) FROM (SELECT provider_transfer_id FROM affiliate_payout_requests WHERE provider_transfer_id IS NOT NULL GROUP BY provider_transfer_id HAVING count(*) > 1) t",
                "negative_reserved_accounts": "SELECT count(*) FROM (SELECT participant_id FROM affiliate_ledger_entries WHERE account = 'reserved' GROUP BY participant_id HAVING sum(amount_cents) < 0) t",
                "unprocessed_billing_events": "SELECT count(*) FROM billing_events WHERE provider IN ('asaas', 'internal_credit') AND status = 'received'",
                "unprocessed_billing_older_than_day": "SELECT count(*) FROM billing_events WHERE provider IN ('asaas', 'internal_credit') AND status = 'received' AND created_at < now() - interval '1 day'",
                "active_policies": "SELECT count(DISTINCT mode) FROM affiliate_policies WHERE status = 'active' AND effective_at <= now()",
                "processing_payouts": "SELECT count(*) FROM affiliate_payout_requests WHERE status = 'processing'",
            }
            for key, query in queries.items():
                report[key] = await conn.scalar(text(query))
            migrated = bool(
                await conn.scalar(
                    text("SELECT to_regclass('affiliate_credit_checkouts')")
                )
            )
            report["credit_migration_present"] = migrated
            if migrated:
                report["unbound_credit_reservations"] = await conn.scalar(
                    text(
                        "SELECT count(*) FROM affiliate_credit_checkouts WHERE state = 'reserved' AND source_payment_id IS NULL"
                    )
                )
            if expect_cash:
                report["cash_flag_enabled"] = bool(
                    await conn.scalar(
                        text(
                            "SELECT enabled_global FROM feature_flags WHERE key = 'affiliate_cash_payouts'"
                        )
                    )
                )
                report["cash_master_enabled"] = os.getenv(
                    "AFFILIATE_CASH_PAYOUTS_ENABLED", ""
                ).lower() in {"true", "1"}
                try:
                    Fernet(os.getenv("AFFILIATE_PAYOUT_ENCRYPTION_KEY", "").encode())
                    report["encryption_key_valid"] = True
                except (ValueError, TypeError):
                    report["encryption_key_valid"] = False
        blockers = bool(
            report["duplicate_transfer_ids"]
            or report["negative_reserved_accounts"]
            or not migrated
            or report["active_policies"] != 2
            or report.get("unbound_credit_reservations", 0)
            or report["unprocessed_billing_older_than_day"]
        )
        if expect_cash:
            blockers |= not all(
                report[key]
                for key in (
                    "cash_flag_enabled",
                    "cash_master_enabled",
                    "encryption_key_valid",
                )
            )
        report["requires_attention"] = blockers
        print(json.dumps(report, indent=2))
        return int(blockers)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-cash-enabled", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(check(args.expect_cash_enabled)))
