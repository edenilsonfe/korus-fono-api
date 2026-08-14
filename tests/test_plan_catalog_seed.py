"""Plan catalog seed — canonical slugs and duplicate deactivation."""

import pytest
from sqlalchemy import select

from app.models.billing import Plan
from app.services.plan_catalog_seed import CANONICAL_PLAN_SLUGS, seed_plan_catalog


@pytest.mark.asyncio
async def test_seed_deactivates_non_canonical_plans(db_session):
    db_session.add(
        Plan(
            slug="legacy_monthly",
            name="Legado",
            price_cents=1000,
            billing_interval="monthly",
            is_active=True,
        )
    )
    db_session.add(
        Plan(
            slug="korusfono_pro_monthly",
            name="KorusFono Pro",
            price_cents=9700,
            billing_interval="monthly",
            is_active=True,
        )
    )
    await db_session.commit()

    await seed_plan_catalog(db_session)
    await db_session.commit()

    rows = await db_session.execute(select(Plan))
    plans = {p.slug: p for p in rows.scalars().all()}

    assert plans["korusfono_pro_monthly"].is_active is True
    assert plans["korusfono_pro_monthly"].price_cents == 9790
    assert plans["legacy_monthly"].is_active is False
    assert len(CANONICAL_PLAN_SLUGS) == 2
