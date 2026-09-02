"""Conversion and checkout reservation of internal KorusFono credit."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate import AffiliateLedgerEntry, AffiliateParticipant
from app.models.professional import Professional


class AffiliateCreditError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AffiliateCreditForbiddenError(AffiliateCreditError):
    pass


@dataclass(slots=True)
class AffiliateCreditReservation:
    participant_id: UUID | None
    reservation_id: str
    original_charge_cents: int
    applied_cents: int
    external_charge_cents: int


class AffiliateCreditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _participant(self, professional_id: UUID, *, lock: bool = False):
        stmt = select(AffiliateParticipant).where(
            AffiliateParticipant.professional_id == professional_id
        )
        if lock:
            stmt = stmt.with_for_update(of=AffiliateParticipant)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _account_balance(self, participant_id: UUID, account: str) -> int:
        amount = await self.db.scalar(
            select(func.coalesce(func.sum(AffiliateLedgerEntry.amount_cents), 0)).where(
                AffiliateLedgerEntry.participant_id == participant_id,
                AffiliateLedgerEntry.account == account,
            )
        )
        return int(amount or 0)

    async def credit_balance(self, professional_id: UUID) -> int:
        participant = await self._participant(professional_id)
        if participant is None:
            return 0
        return await self._account_balance(participant.id, "credit")

    async def convert_available_to_credit(
        self,
        *,
        professional: Professional,
        amount_cents: int,
        idempotency_key: str,
    ) -> int:
        if amount_cents <= 0:
            raise AffiliateCreditForbiddenError("Informe um valor positivo")
        participant = await self._participant(professional.id, lock=True)
        if (
            participant is None
            or participant.status != "active"
            or not participant.customer_enabled
        ):
            raise AffiliateCreditForbiddenError("Participação de cliente não está ativa")
        prior = await self.db.scalar(
            select(func.count()).select_from(AffiliateLedgerEntry).where(
                AffiliateLedgerEntry.idempotency_key == f"{idempotency_key}:credit"
            )
        )
        if prior:
            return amount_cents
        available = await self._account_balance(participant.id, "available")
        if available < 0:
            raise AffiliateCreditForbiddenError(
                "Saldo negativo precisa ser compensado antes de usar crédito"
            )
        if amount_cents > available:
            raise AffiliateCreditForbiddenError("Saldo disponível insuficiente")
        self.db.add_all(
            [
                AffiliateLedgerEntry(
                    participant_id=participant.id,
                    entry_type="credit_conversion",
                    account="available",
                    amount_cents=-amount_cents,
                    idempotency_key=f"{idempotency_key}:available",
                ),
                AffiliateLedgerEntry(
                    participant_id=participant.id,
                    entry_type="credit_conversion",
                    account="credit",
                    amount_cents=amount_cents,
                    idempotency_key=f"{idempotency_key}:credit",
                ),
            ]
        )
        await self.db.flush()
        return amount_cents

    async def reserve_for_checkout(
        self,
        *,
        professional_id: UUID,
        charge_cents: int,
        reservation_id: str,
    ) -> AffiliateCreditReservation:
        participant = await self._participant(professional_id, lock=True)
        if participant is None or participant.status != "active":
            return AffiliateCreditReservation(
                participant_id=None,
                reservation_id=reservation_id,
                original_charge_cents=charge_cents,
                applied_cents=0,
                external_charge_cents=charge_cents,
            )
        existing = (
            await self.db.execute(
                select(AffiliateLedgerEntry).where(
                    AffiliateLedgerEntry.idempotency_key
                    == f"credit-reservation:{reservation_id}:credit"
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            applied = -existing.amount_cents
            return AffiliateCreditReservation(
                participant_id=participant.id,
                reservation_id=reservation_id,
                original_charge_cents=charge_cents,
                applied_cents=applied,
                external_charge_cents=max(0, charge_cents - applied),
            )
        credit = max(0, await self._account_balance(participant.id, "credit"))
        applied = min(max(charge_cents, 0), credit)
        if applied:
            metadata = {"reservationId": reservation_id, "reservationKind": "credit"}
            self.db.add_all(
                [
                    AffiliateLedgerEntry(
                        participant_id=participant.id,
                        entry_type="credit_checkout_reserved",
                        account="credit",
                        amount_cents=-applied,
                        idempotency_key=f"credit-reservation:{reservation_id}:credit",
                        metadata_json=metadata,
                    ),
                    AffiliateLedgerEntry(
                        participant_id=participant.id,
                        entry_type="credit_checkout_reserved",
                        account="reserved",
                        amount_cents=applied,
                        idempotency_key=f"credit-reservation:{reservation_id}:reserved",
                        metadata_json=metadata,
                    ),
                ]
            )
            await self.db.flush()
        return AffiliateCreditReservation(
            participant_id=participant.id,
            reservation_id=reservation_id,
            original_charge_cents=charge_cents,
            applied_cents=applied,
            external_charge_cents=max(0, charge_cents - applied),
        )

    async def _reservation_entry(self, reservation_id: str) -> AffiliateLedgerEntry | None:
        return (
            await self.db.execute(
                select(AffiliateLedgerEntry).where(
                    AffiliateLedgerEntry.idempotency_key
                    == f"credit-reservation:{reservation_id}:reserved"
                )
            )
        ).scalar_one_or_none()

    async def release_checkout_reservation(self, *, reservation_id: str) -> int:
        reservation = await self._reservation_entry(reservation_id)
        if reservation is None:
            return 0
        existing = await self.db.scalar(
            select(func.count()).select_from(AffiliateLedgerEntry).where(
                AffiliateLedgerEntry.idempotency_key
                == f"credit-reservation:{reservation_id}:released-credit"
            )
        )
        if existing:
            return reservation.amount_cents
        amount = reservation.amount_cents
        self.db.add_all(
            [
                AffiliateLedgerEntry(
                    participant_id=reservation.participant_id,
                    entry_type="credit_checkout_released",
                    account="reserved",
                    amount_cents=-amount,
                    idempotency_key=f"credit-reservation:{reservation_id}:released-reserved",
                ),
                AffiliateLedgerEntry(
                    participant_id=reservation.participant_id,
                    entry_type="credit_checkout_released",
                    account="credit",
                    amount_cents=amount,
                    idempotency_key=f"credit-reservation:{reservation_id}:released-credit",
                ),
            ]
        )
        await self.db.flush()
        return amount

    async def settle_checkout_reservation(self, *, reservation_id: str) -> int:
        reservation = await self._reservation_entry(reservation_id)
        if reservation is None:
            return 0
        existing = await self.db.scalar(
            select(func.count()).select_from(AffiliateLedgerEntry).where(
                AffiliateLedgerEntry.idempotency_key
                == f"credit-reservation:{reservation_id}:settled"
            )
        )
        if existing:
            return reservation.amount_cents
        self.db.add(
            AffiliateLedgerEntry(
                participant_id=reservation.participant_id,
                entry_type="credit_checkout_settled",
                account="reserved",
                amount_cents=-reservation.amount_cents,
                idempotency_key=f"credit-reservation:{reservation_id}:settled",
            )
        )
        await self.db.flush()
        return reservation.amount_cents
