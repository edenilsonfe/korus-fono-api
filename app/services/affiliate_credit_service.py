"""Conversion and checkout reservation of internal KorusFono credit."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate import (
    AffiliateCreditCheckout,
    AffiliateLedgerEntry,
    AffiliateParticipant,
)
from app.models.professional import Professional
from app.services.affiliate_accounting import lock_participant


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
    reused: bool = False
    payment_id: str | None = None


class AffiliateCreditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _participant(self, professional_id: UUID, *, lock: bool = False):
        stmt = select(AffiliateParticipant).where(
            AffiliateParticipant.professional_id == professional_id
        )
        if lock:
            stmt = stmt.with_for_update(of=AffiliateParticipant).execution_options(
                populate_existing=True
            )
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
            raise AffiliateCreditForbiddenError(
                "Participação de cliente não está ativa"
            )
        prior = await self.db.scalar(
            select(AffiliateLedgerEntry).where(
                AffiliateLedgerEntry.idempotency_key == f"{idempotency_key}:credit"
            )
        )
        if prior:
            if (
                prior.participant_id != participant.id
                or prior.amount_cents != amount_cents
            ):
                raise AffiliateCreditForbiddenError(
                    "Identificador já utilizado com outro valor"
                )
            return prior.amount_cents
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

    async def _latest(self, reservation_id: str):
        return (
            await self.db.execute(
                select(AffiliateCreditCheckout)
                .where(AffiliateCreditCheckout.reservation_id == reservation_id)
                .order_by(AffiliateCreditCheckout.attempt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _entry(self, row, suffix: str, account: str, amount: int):
        self.db.add(
            AffiliateLedgerEntry(
                participant_id=row.participant_id,
                account=account,
                amount_cents=amount,
                entry_type=f"credit_checkout_{suffix}",
                idempotency_key=f"credit-checkout:{row.id}:{suffix}:{account}",
                metadata_json={
                    "reservationId": row.reservation_id,
                    "attempt": row.attempt,
                },
            )
        )

    async def reserve_for_checkout(
        self, *, professional_id: UUID, charge_cents: int, reservation_id: str
    ) -> AffiliateCreditReservation:
        participant = await self._participant(professional_id, lock=True)
        applied = 0
        reused = False
        payment_id = None
        if participant is not None and participant.status == "active":
            row = await self._latest(reservation_id)
            if row and row.participant_id != participant.id:
                raise AffiliateCreditForbiddenError("Reserva vinculada a outra conta")
            if row and row.state == "settled":
                raise AffiliateCreditForbiddenError(
                    "O crédito deste checkout já foi liquidado"
                )
            if row and row.state == "reserved":
                if row.charge_cents != charge_cents:
                    raise AffiliateCreditForbiddenError(
                        "Finalize a cobrança existente antes de alterar o valor"
                    )
                applied = row.amount_cents
                reused = True
                payment_id = row.source_payment_id
            else:
                # Never silently reinterpret a pre-migration reservation.
                legacy = await self.db.scalar(
                    select(AffiliateLedgerEntry.id).where(
                        AffiliateLedgerEntry.idempotency_key
                        == f"credit-reservation:{reservation_id}:reserved"
                    )
                )
                if legacy and row is None:
                    raise AffiliateCreditForbiddenError(
                        "Reserva anterior requer conciliação antes de reutilizar o crédito"
                    )
                available = await self._account_balance(participant.id, "available")
                credit = max(0, await self._account_balance(participant.id, "credit"))
                applied = min(max(charge_cents, 0), credit) if available >= 0 else 0
                if applied:
                    row = AffiliateCreditCheckout(
                        participant_id=participant.id,
                        reservation_id=reservation_id,
                        attempt=row.attempt + 1 if row else 1,
                        amount_cents=applied,
                        charge_cents=charge_cents,
                        state="reserved",
                    )
                    self.db.add(row)
                    await self.db.flush()
                    await self._entry(row, "reserved", "credit", -applied)
                    await self._entry(row, "reserved", "reserved", applied)
                    await self.db.flush()
        return AffiliateCreditReservation(
            participant.id if participant else None,
            reservation_id,
            charge_cents,
            applied,
            max(0, charge_cents - applied),
            reused,
            payment_id,
        )

    async def bind_payment(self, *, reservation_id: str, payment_id: str) -> None:
        row = await self._latest(reservation_id)
        if row is None:
            return
        await lock_participant(self.db, row.participant_id)
        await self.db.refresh(row)
        if row.source_payment_id and row.source_payment_id != payment_id:
            raise AffiliateCreditForbiddenError("Reserva já vinculada a outra cobrança")
        row.source_payment_id = payment_id
        await self.db.flush()

    async def _locked(self, reservation_id: str, payment_id: str | None = None):
        row = (
            (
                await self.db.execute(
                    select(AffiliateCreditCheckout).where(
                        AffiliateCreditCheckout.source_payment_id == payment_id
                    )
                )
            ).scalar_one_or_none()
            if payment_id
            else await self._latest(reservation_id)
        )
        if row is None and payment_id and reservation_id:
            candidate = await self._latest(reservation_id)
            if candidate:
                await lock_participant(self.db, candidate.participant_id)
                await self.db.refresh(candidate)
                # Transparent card/Pix may replace the original hosted checkout.
                # Only the first financially settled attempt may consume credit.
                if candidate.state == "reserved":
                    candidate.source_payment_id = payment_id
                    row = candidate
        if row:
            await lock_participant(self.db, row.participant_id)
            await self.db.refresh(row)
        return row

    async def release_checkout_reservation(
        self,
        *,
        reservation_id: str,
        payment_id: str | None = None,
        refund_bps: int = 10000,
    ) -> int:
        row = await self._locked(reservation_id, payment_id)
        if row is None or row.state in {"released", "refunded"}:
            return 0
        if row.state == "reserved" and payment_id is None:
            if row.source_payment_id:
                raise AffiliateCreditForbiddenError(
                    "Concilie a cobrança vinculada antes de liberar o crédito"
                )
            await self._entry(row, "released", "reserved", -row.amount_cents)
            await self._entry(row, "released", "credit", row.amount_cents)
            row.state = "released"
            await self.db.flush()
            return row.amount_cents
        # A financial refund settles the reservation first, then returns credit;
        # it cannot subtract the reserved balance twice.
        if row.state == "reserved":
            await self._entry(row, "settled", "reserved", -row.amount_cents)
            row.state = "settled"
        target = row.amount_cents * max(0, min(refund_bps, 10000)) // 10000
        delta = max(0, target - row.refunded_cents)
        if delta:
            await self._entry(row, f"refund-{target}", "credit", delta)
            row.refunded_cents = target
        if row.refunded_cents == row.amount_cents:
            row.state = "refunded"
        await self.db.flush()
        return delta

    async def settle_checkout_reservation(
        self, *, reservation_id: str, payment_id: str | None = None
    ) -> int:
        row = await self._locked(reservation_id, payment_id)
        if row is None or row.state != "reserved":
            return 0
        await self._entry(row, "settled", "reserved", -row.amount_cents)
        row.state = "settled"
        await self.db.flush()
        return row.amount_cents
