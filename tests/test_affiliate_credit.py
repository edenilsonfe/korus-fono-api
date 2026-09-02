"""KorusFono credit redemption and checkout reservation contracts."""

from datetime import UTC, datetime

import pytest

from app.models.affiliate import AffiliateLedgerEntry, AffiliateParticipant
from app.models.professional import Professional
from app.services.affiliate_credit_service import (
    AffiliateCreditForbiddenError,
    AffiliateCreditService,
)

pytestmark = pytest.mark.asyncio


async def test_available_reward_can_be_converted_to_credit_and_reserved(db_session):
    professional = Professional(
        email="credit-customer@example.com",
        password_hash="unused",
        name="Cliente crédito",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
        subscription_status="active",
    )
    db_session.add(professional)
    await db_session.flush()
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        status="active",
        customer_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_available",
            account="available",
            amount_cents=5000,
            idempotency_key="test:credit:available",
        )
    )
    await db_session.commit()

    service = AffiliateCreditService(db_session)
    converted = await service.convert_available_to_credit(
        professional=professional,
        amount_cents=5000,
        idempotency_key="redeem:credit:1",
    )
    assert converted == 5000
    assert await service.credit_balance(professional.id) == 5000

    reservation = await service.reserve_for_checkout(
        professional_id=professional.id,
        charge_cents=9700,
        reservation_id="checkout-credit-1",
    )
    assert reservation.applied_cents == 5000
    assert reservation.external_charge_cents == 4700
    assert await service.credit_balance(professional.id) == 0

    await service.release_checkout_reservation(reservation_id="checkout-credit-1")
    assert await service.credit_balance(professional.id) == 5000


async def test_negative_available_balance_blocks_credit_conversion(db_session):
    professional = Professional(
        email="negative-credit@example.com",
        password_hash="unused",
        name="Saldo negativo",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
        subscription_status="active",
    )
    db_session.add(professional)
    await db_session.flush()
    participant = AffiliateParticipant(
        professional_id=professional.id,
        email=professional.email,
        status="active",
        customer_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_negative",
            account="available",
            amount_cents=-100,
            idempotency_key="test:credit:negative",
        )
    )
    await db_session.commit()

    with pytest.raises(AffiliateCreditForbiddenError, match="negativo"):
        await AffiliateCreditService(db_session).convert_available_to_credit(
            professional=professional,
            amount_cents=100,
            idempotency_key="redeem:credit:blocked",
        )
