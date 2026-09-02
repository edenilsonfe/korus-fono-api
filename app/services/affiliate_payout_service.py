"""Fiscal profiles and dual-control affiliate payout batches."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.affiliate import (
    AffiliateFiscalProfile,
    AffiliateLedgerEntry,
    AffiliateParticipant,
    AffiliatePayoutBatch,
    AffiliatePayoutRequest,
    AffiliateReferral,
)
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.affiliate_notification_service import AffiliateNotificationService


class AffiliatePayoutError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AffiliatePayoutForbiddenError(AffiliatePayoutError):
    pass


class AffiliatePayoutConflictError(AffiliatePayoutError):
    pass


class AffiliatePayoutNotFoundError(AffiliatePayoutError):
    pass


def get_affiliate_fernet_key() -> str:
    return get_settings().affiliate_payout_encryption_key.strip()


def _fernet() -> Fernet:
    raw = get_affiliate_fernet_key()
    if not raw:
        raise AffiliatePayoutForbiddenError(
            "Pagamentos em dinheiro ainda não estão configurados"
        )
    try:
        return Fernet(raw.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise AffiliatePayoutForbiddenError(
            "Configuração de segurança de pagamentos inválida"
        ) from exc


def _fingerprint(value: str) -> str:
    key = get_affiliate_fernet_key().encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _mask(value: str, visible: int = 4) -> str:
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _next_monday_cutoff(now: datetime) -> datetime:
    try:
        sao_paulo = ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:  # Windows dev env without the optional tzdata wheel.
        sao_paulo = timezone(timedelta(hours=-3))
    local_now = _as_utc(now).astimezone(sao_paulo)
    days = (7 - local_now.weekday()) % 7
    cutoff_date = local_now.date() + timedelta(days=days)
    cutoff = datetime.combine(cutoff_date, time(12, 0), tzinfo=sao_paulo)
    if cutoff <= local_now:
        cutoff += timedelta(days=7)
    return cutoff.astimezone(UTC)


class AffiliatePayoutService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _referred_billing_documents(self, participant_id: UUID) -> set[str]:
        rows = (
            await self.db.execute(
                select(
                    Professional.cpf,
                    Professional.billing_cnpj,
                    Subscription.billing_document,
                )
                .select_from(AffiliateReferral)
                .join(
                    Professional,
                    Professional.id == AffiliateReferral.referred_professional_id,
                )
                .outerjoin(
                    Subscription,
                    Subscription.professional_id == Professional.id,
                )
                .where(AffiliateReferral.participant_id == participant_id)
            )
        ).all()
        return {
            normalized
            for row in rows
            for value in row
            if (normalized := _digits(value))
        }

    async def _refresh_batch_status(self, batch_id: UUID | None) -> None:
        if batch_id is None:
            return
        batch = await self.db.get(AffiliatePayoutBatch, batch_id)
        if batch is None:
            return
        statuses = (
            await self.db.execute(
                select(AffiliatePayoutRequest.status).where(
                    AffiliatePayoutRequest.batch_id == batch_id
                )
            )
        ).scalars().all()
        if statuses and all(status == "paid" for status in statuses):
            batch.status = "paid"
        elif statuses and all(status in {"paid", "failed"} for status in statuses):
            batch.status = "failed"
        elif any(status == "processing" for status in statuses):
            batch.status = "processing"

    async def submit_fiscal_profile(
        self,
        *,
        participant: AffiliateParticipant,
        person_type: str,
        legal_name: str,
        document: str,
        pix_key_type: str,
        pix_key: str,
    ) -> AffiliateFiscalProfile:
        if person_type not in {"pf", "pj"}:
            raise AffiliatePayoutConflictError("Tipo de pessoa inválido")
        normalized_document = _digits(document)
        expected = 11 if person_type == "pf" else 14
        if len(normalized_document) != expected:
            raise AffiliatePayoutConflictError("Documento fiscal inválido")
        normalized_pix_type = pix_key_type.strip().lower()
        normalized_pix = (
            _digits(pix_key) if normalized_pix_type in {"cpf", "cnpj"} else pix_key.strip()
        )
        if not normalized_pix:
            raise AffiliatePayoutConflictError("Chave Pix obrigatória")
        if normalized_pix_type == "cpf" and len(normalized_pix) != 11:
            raise AffiliatePayoutConflictError("Chave Pix CPF inválida")
        if normalized_pix_type == "cnpj" and len(normalized_pix) != 14:
            raise AffiliatePayoutConflictError("Chave Pix CNPJ inválida")
        referred_documents = await self._referred_billing_documents(participant.id)
        if normalized_document in referred_documents or (
            normalized_pix_type in {"cpf", "cnpj"}
            and normalized_pix in referred_documents
        ):
            raise AffiliatePayoutForbiddenError(
                "Autoindicação detectada pelo documento fiscal ou titular da chave Pix"
            )
        current = (
            await self.db.execute(
                select(AffiliateFiscalProfile)
                .where(AffiliateFiscalProfile.participant_id == participant.id)
                .order_by(AffiliateFiscalProfile.version.desc())
            )
        ).scalars().first()
        if current is not None:
            current.status = "superseded"
        version = (current.version if current else 0) + 1
        cipher = _fernet()
        now = datetime.now(UTC)
        profile = AffiliateFiscalProfile(
            participant_id=participant.id,
            version=version,
            person_type=person_type,
            status="pending",
            legal_name=legal_name.strip(),
            document_fingerprint=_fingerprint(normalized_document),
            document_masked=_mask(normalized_document),
            encrypted_document=cipher.encrypt(normalized_document.encode()).decode(),
            pix_key_type=normalized_pix_type,
            pix_key_masked=_mask(normalized_pix),
            pix_key_fingerprint=_fingerprint(normalized_pix.lower()),
            encrypted_pix_key=cipher.encrypt(normalized_pix.encode()).decode(),
            withdrawal_locked_until=now + timedelta(hours=48),
        )
        self.db.add(profile)
        await self.db.flush()
        return profile

    async def approve_fiscal_profile(
        self, *, profile_id: UUID, actor: Professional, pix_validated: bool
    ) -> AffiliateFiscalProfile:
        profile = await self.db.get(AffiliateFiscalProfile, profile_id)
        if profile is None:
            raise AffiliatePayoutNotFoundError("Perfil fiscal não encontrado")
        if not pix_validated:
            raise AffiliatePayoutConflictError("Valide a titularidade da chave Pix")
        profile.status = "approved"
        profile.pix_validated_at = datetime.now(UTC)
        profile.approved_at = datetime.now(UTC)
        profile.approved_by_id = actor.id
        await self.db.flush()
        return profile

    async def _approved_profile(self, participant_id: UUID) -> AffiliateFiscalProfile:
        profile = (
            await self.db.execute(
                select(AffiliateFiscalProfile)
                .where(
                    AffiliateFiscalProfile.participant_id == participant_id,
                    AffiliateFiscalProfile.status == "approved",
                )
                .order_by(AffiliateFiscalProfile.version.desc())
            )
        ).scalars().first()
        if profile is None or profile.pix_validated_at is None:
            raise AffiliatePayoutForbiddenError(
                "Complete e aprove o perfil fiscal antes de sacar"
            )
        lock_until = profile.withdrawal_locked_until
        if lock_until and _as_utc(lock_until) > datetime.now(UTC):
            raise AffiliatePayoutForbiddenError(
                "Saques ficam bloqueados por 48 horas após alterar a chave Pix"
            )
        referred_documents = await self._referred_billing_documents(participant_id)
        referred_fingerprints = {_fingerprint(value) for value in referred_documents}
        if profile.document_fingerprint in referred_fingerprints or (
            profile.pix_key_type in {"cpf", "cnpj"}
            and profile.pix_key_fingerprint in referred_fingerprints
        ):
            raise AffiliatePayoutForbiddenError(
                "Autoindicação detectada pelo documento fiscal ou titular da chave Pix"
            )
        return profile

    async def _available(self, participant_id: UUID) -> int:
        value = await self.db.scalar(
            select(func.coalesce(func.sum(AffiliateLedgerEntry.amount_cents), 0)).where(
                AffiliateLedgerEntry.participant_id == participant_id,
                AffiliateLedgerEntry.account == "available",
            )
        )
        return int(value or 0)

    async def request_cash_payout(
        self,
        *,
        participant: AffiliateParticipant,
        amount_cents: int,
        cash_enabled: bool,
        withholding_cents: int = 0,
    ) -> AffiliatePayoutRequest:
        if not cash_enabled:
            raise AffiliatePayoutForbiddenError("Saques em dinheiro ainda não estão disponíveis")
        if participant.status != "active":
            raise AffiliatePayoutForbiddenError("Participante não está ativo")
        if amount_cents < 10000:
            raise AffiliatePayoutForbiddenError("O saque mínimo bruto é de R$ 100")
        available = await self._available(participant.id)
        if available < 0 or amount_cents > available:
            raise AffiliatePayoutForbiddenError("Saldo disponível insuficiente")
        if withholding_cents < 0 or withholding_cents > amount_cents:
            raise AffiliatePayoutConflictError("Retenção inválida")
        profile = await self._approved_profile(participant.id)
        now = datetime.now(UTC)
        payout = AffiliatePayoutRequest(
            participant_id=participant.id,
            fiscal_profile_id=profile.id,
            status="requested",
            gross_cents=amount_cents,
            withholding_cents=withholding_cents,
            fee_cents=0,
            net_cents=amount_cents - withholding_cents,
            requested_at=now,
            cancellable_until=_next_monday_cutoff(now),
        )
        self.db.add(payout)
        await self.db.flush()
        self.db.add_all(
            [
                AffiliateLedgerEntry(
                    participant_id=participant.id,
                    payout_request_id=payout.id,
                    entry_type="payout_reserved",
                    account="available",
                    amount_cents=-amount_cents,
                    idempotency_key=f"payout:{payout.id}:available-reserved",
                ),
                AffiliateLedgerEntry(
                    participant_id=participant.id,
                    payout_request_id=payout.id,
                    entry_type="payout_reserved",
                    account="reserved",
                    amount_cents=amount_cents,
                    idempotency_key=f"payout:{payout.id}:reserved",
                ),
            ]
        )
        await self.db.flush()
        return payout

    async def cancel_payout(
        self, *, payout_id: UUID, participant_id: UUID, now: datetime | None = None
    ) -> AffiliatePayoutRequest:
        payout = await self.db.get(AffiliatePayoutRequest, payout_id)
        if payout is None or payout.participant_id != participant_id:
            raise AffiliatePayoutNotFoundError("Solicitação de saque não encontrada")
        current = now or datetime.now(UTC)
        if payout.status != "requested" or _as_utc(payout.cancellable_until) <= _as_utc(current):
            raise AffiliatePayoutConflictError("O prazo para cancelar este saque terminou")
        payout.status = "canceled"
        self.db.add_all(
            [
                AffiliateLedgerEntry(
                    participant_id=participant_id,
                    payout_request_id=payout.id,
                    entry_type="payout_canceled",
                    account="reserved",
                    amount_cents=-payout.gross_cents,
                    idempotency_key=f"payout:{payout.id}:cancel-reserved",
                ),
                AffiliateLedgerEntry(
                    participant_id=participant_id,
                    payout_request_id=payout.id,
                    entry_type="payout_canceled",
                    account="available",
                    amount_cents=payout.gross_cents,
                    idempotency_key=f"payout:{payout.id}:cancel-available",
                ),
            ]
        )
        await self.db.flush()
        return payout

    async def create_weekly_batch(
        self, *, actor: Professional, now: datetime
    ) -> AffiliatePayoutBatch:
        eligible = (
            await self.db.execute(
                select(AffiliatePayoutRequest).where(
                    AffiliatePayoutRequest.status == "requested",
                    AffiliatePayoutRequest.cancellable_until <= now,
                )
            )
        ).scalars().all()
        if not eligible:
            raise AffiliatePayoutConflictError("Nenhum saque elegível para o lote")
        try:
            sao_paulo = ZoneInfo("America/Sao_Paulo")
        except ZoneInfoNotFoundError:
            sao_paulo = timezone(timedelta(hours=-3))
        local = _as_utc(now).astimezone(sao_paulo)
        batch = AffiliatePayoutBatch(
            competence=local.strftime("%Y-%m"),
            status="draft",
            cutoff_at=now,
            prepared_by_id=actor.id,
        )
        self.db.add(batch)
        await self.db.flush()
        for payout in eligible:
            payout.status = "batched"
            payout.batch_id = batch.id
        await self.db.flush()
        return batch

    async def approve_batch(
        self,
        *,
        batch_id: UUID,
        actor: Professional,
        allow_single_operator: bool,
    ) -> AffiliatePayoutBatch:
        batch = await self.db.get(AffiliatePayoutBatch, batch_id)
        if batch is None:
            raise AffiliatePayoutNotFoundError("Lote não encontrado")
        if batch.status != "draft":
            raise AffiliatePayoutConflictError("Lote não está aguardando aprovação")
        if batch.prepared_by_id == actor.id and not allow_single_operator:
            raise AffiliatePayoutConflictError("A aprovação exige uma segunda pessoa")
        batch.status = "approved"
        batch.approved_by_id = actor.id
        batch.approved_at = datetime.now(UTC)
        payouts = (
            await self.db.execute(
                select(AffiliatePayoutRequest).where(
                    AffiliatePayoutRequest.batch_id == batch.id
                )
            )
        ).scalars().all()
        for payout in payouts:
            payout.status = "approved"
        await self.db.flush()
        return batch

    async def mark_transfer_processing(
        self, *, payout_id: UUID, provider_transfer_id: str
    ) -> AffiliatePayoutRequest:
        payout = await self.db.get(AffiliatePayoutRequest, payout_id)
        if payout is None:
            raise AffiliatePayoutNotFoundError("Saque não encontrado")
        if payout.status != "approved":
            raise AffiliatePayoutConflictError("Saque não está aprovado")
        payout.status = "processing"
        payout.provider_transfer_id = provider_transfer_id.strip()
        await self.db.flush()
        await self._refresh_batch_status(payout.batch_id)
        return payout

    async def complete_transfer(
        self, *, provider_transfer_id: str, succeeded: bool, failure_reason: str | None = None
    ) -> AffiliatePayoutRequest | None:
        payout = (
            await self.db.execute(
                select(AffiliatePayoutRequest).where(
                    AffiliatePayoutRequest.provider_transfer_id == provider_transfer_id
                )
            )
        ).scalar_one_or_none()
        if payout is None or payout.status in {"paid", "failed"}:
            return payout
        payout.processed_at = datetime.now(UTC)
        if succeeded:
            payout.status = "paid"
            self.db.add(
                AffiliateLedgerEntry(
                    participant_id=payout.participant_id,
                    payout_request_id=payout.id,
                    entry_type="payout_paid",
                    account="reserved",
                    amount_cents=-payout.gross_cents,
                    idempotency_key=f"payout:{payout.id}:paid",
                )
            )
        else:
            payout.status = "failed"
            payout.failure_reason = (failure_reason or "Falha na transferência")[:500]
            self.db.add_all(
                [
                    AffiliateLedgerEntry(
                        participant_id=payout.participant_id,
                        payout_request_id=payout.id,
                        entry_type="payout_failed",
                        account="reserved",
                        amount_cents=-payout.gross_cents,
                        idempotency_key=f"payout:{payout.id}:failed-reserved",
                    ),
                    AffiliateLedgerEntry(
                        participant_id=payout.participant_id,
                        payout_request_id=payout.id,
                        entry_type="payout_failed",
                        account="available",
                        amount_cents=payout.gross_cents,
                        idempotency_key=f"payout:{payout.id}:failed-available",
                    ),
                ]
            )
        await self.db.flush()
        await self._refresh_batch_status(payout.batch_id)
        participant = await self.db.get(AffiliateParticipant, payout.participant_id)
        if participant is not None:
            await AffiliateNotificationService(self.db).notify(
                participant=participant,
                event_type="affiliate_payout",
                title="Saque pago" if succeeded else "Falha no saque",
                body=(
                    "A transferência Pix foi concluída pelo provedor."
                    if succeeded
                    else "O valor voltou ao saldo disponível. Revise a chave Pix antes de solicitar novamente."
                ),
                severity="success" if succeeded else "warning",
            )
        await self.db.flush()
        return payout
