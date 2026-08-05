"""Remap legacy resource categories to the curated fono taxonomy.

Idempotent: run multiple times safely. Run BEFORE any write to old rows
(otherwise Pydantic rejects legacy categories on edit).
Usage: uv run python -m scripts.remap_resource_categories
"""
import asyncio

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.resource import Resource

LEGACY_DROP = {
    "Fonoaudiologia",
    "Terapia Ocupacional",
    "Psicologia",
    "Fisioterapia",
    "Psicopedagogia",
}
LEGACY_FALLBACK = "Linguagem"


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Resource))
        items = list(result.scalars().all())
        changed = 0
        for item in items:
            cats = [c for c in (item.categories or []) if c not in LEGACY_DROP]
            if not cats:
                cats = [LEGACY_FALLBACK]
            if cats != item.categories:
                item.categories = cats
                changed += 1
        await db.commit()
        print(f"Remapped {changed}/{len(items)} resources")


if __name__ == "__main__":
    asyncio.run(main())
