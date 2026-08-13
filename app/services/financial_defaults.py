"""Default configuration for the professional's internal finance ledger."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import FinancialCategory, PaymentMethod

DEFAULT_FINANCIAL_CATEGORIES = (
    ("income", "Atendimentos"),
    ("income", "Avaliações"),
    ("income", "Pacotes"),
    ("income", "Taxas de cancelamento"),
    ("income", "Outras receitas"),
    ("expense", "Aluguel e estrutura"),
    ("expense", "Materiais e insumos"),
    ("expense", "Serviços de terceiros"),
    ("expense", "Impostos e taxas"),
    ("expense", "Outras despesas"),
)

DEFAULT_PAYMENT_METHOD_NAMES = (
    "Cartão de crédito",
    "Cartão de débito",
    "Pix",
    "Dinheiro",
)


def add_default_payment_methods(db: AsyncSession, professional_id: UUID) -> list[PaymentMethod]:
    """Stage all defaults for a newly-created professional in its current transaction."""
    methods = [
        PaymentMethod(professional_id=professional_id, name=name)
        for name in DEFAULT_PAYMENT_METHOD_NAMES
    ]
    db.add_all(methods)
    return methods


def add_default_financial_categories(
    db: AsyncSession, professional_id: UUID
) -> list[FinancialCategory]:
    """Stage the clinic's editable income and expense categories for a new account."""
    categories = [
        FinancialCategory(professional_id=professional_id, kind=kind, name=name)
        for kind, name in DEFAULT_FINANCIAL_CATEGORIES
    ]
    db.add_all(categories)
    return categories


async def ensure_default_financial_categories(
    db: AsyncSession, professional_id: UUID
) -> None:
    """Idempotently backfill categories while preserving custom and inactive records."""
    result = await db.execute(
        select(FinancialCategory.kind, FinancialCategory.name).where(
            FinancialCategory.professional_id == professional_id
        )
    )
    existing = {(kind, name.casefold()) for kind, name in result.all()}
    missing = [
        (kind, name)
        for kind, name in DEFAULT_FINANCIAL_CATEGORIES
        if (kind, name.casefold()) not in existing
    ]
    if not missing:
        return

    db.add_all(
        [
            FinancialCategory(professional_id=professional_id, kind=kind, name=name)
            for kind, name in missing
        ]
    )
    try:
        await db.commit()
    except IntegrityError:
        # Another first access can perform the same backfill concurrently.
        await db.rollback()


async def ensure_default_payment_methods(db: AsyncSession, professional_id: UUID) -> None:
    """Idempotently backfill defaults for accounts created outside registration."""
    result = await db.execute(
        select(PaymentMethod.name).where(PaymentMethod.professional_id == professional_id)
    )
    existing = {name.casefold() for name in result.scalars().all()}
    missing = [name for name in DEFAULT_PAYMENT_METHOD_NAMES if name.casefold() not in existing]
    if not missing:
        return

    db.add_all([PaymentMethod(professional_id=professional_id, name=name) for name in missing])
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent first access may have inserted the same defaults. The
        # unique constraint is authoritative and the following list query will
        # observe the committed records.
        await db.rollback()
