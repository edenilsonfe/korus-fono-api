"""Seed the commercial billing plan catalog idempotently."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan

COMMERCIAL_PLAN_SEEDS: list[dict[str, Any]] = [
    {
        "slug": "korusfono_pro_monthly",
        "name": "KorusFono Pro",
        "description": "Pacientes ilimitados, IA clínica e relatórios automáticos.",
        "limits": {},
        "price_cents": 9790,
        "currency": "BRL",
        "billing_interval": "monthly",
        "features": [
            "Protocolos validados no catálogo",
            "Scoring automático na aplicação",
            "Histórico evolutivo por paciente",
            "Rascunho de laudo com IA",
            "Prontuário, agenda e WhatsApp",
        ],
        "badge": None,
        "highlighted": True,
        "display_order": 1,
        "is_active": True,
    },
    {
        "slug": "korusfono_pro_yearly",
        "name": "KorusFono Pro",
        "description": "Pacientes ilimitados, IA clínica e relatórios automáticos — cobrança anual.",
        "limits": {},
        "price_cents": 97000,
        "currency": "BRL",
        "billing_interval": "yearly",
        "features": [
            "Protocolos validados no catálogo",
            "Scoring automático na aplicação",
            "Histórico evolutivo por paciente",
            "Rascunho de laudo com IA",
            "Prontuário, agenda e WhatsApp",
            "Economia de ~2 meses no plano anual",
        ],
        "badge": "Melhor valor",
        "highlighted": True,
        "display_order": 2,
        "is_active": True,
    },
    {
        "slug": "korusfono_pro_quarterly",
        "name": "KorusFono Pro",
        "description": "Pacientes ilimitados, IA clínica e relatórios automáticos — cobrança trimestral.",
        "limits": {},
        "price_cents": 27900,
        "currency": "BRL",
        "billing_interval": "quarterly",
        "features": [
            "Protocolos validados no catálogo",
            "Scoring automático na aplicação",
            "Histórico evolutivo por paciente",
            "Rascunho de laudo com IA",
            "Prontuário, agenda e WhatsApp",
        ],
        "badge": None,
        "highlighted": False,
        "display_order": 3,
        "is_active": True,
    },
]


CANONICAL_PLAN_SLUGS = frozenset(seed["slug"] for seed in COMMERCIAL_PLAN_SEEDS)

_SYNC_KEYS = (
    "name",
    "description",
    "limits",
    "price_cents",
    "currency",
    "billing_interval",
    "features",
    "badge",
    "highlighted",
    "display_order",
    "is_active",
)


async def seed_plan_catalog(session: AsyncSession) -> None:
    for seed in COMMERCIAL_PLAN_SEEDS:
        result = await session.execute(select(Plan).where(Plan.slug == seed["slug"]))
        row = result.scalar_one_or_none()
        if row:
            for key in _SYNC_KEYS:
                setattr(row, key, seed[key])
        else:
            session.add(Plan(**seed))

    # ponytail: single-product catalog — deactivate legacy/duplicate active plans
    extras = await session.execute(
        select(Plan).where(Plan.is_active.is_(True), Plan.slug.not_in(CANONICAL_PLAN_SLUGS))
    )
    for plan in extras.scalars().all():
        plan.is_active = False
