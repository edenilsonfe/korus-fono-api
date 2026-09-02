"""Affiliate program business rules and append-only accounting."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate import (
    AffiliateCode,
    AffiliateLedgerEntry,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
    AffiliateReward,
)
from app.models.billing import Subscription
from app.models.professional import Professional
from app.services.affiliate_notification_service import AffiliateNotificationService


class AffiliateError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AffiliateNotFoundError(AffiliateError):
    pass


class AffiliateConflictError(AffiliateError):
    pass


class AffiliateForbiddenError(AffiliateError):
    pass


@dataclass(slots=True)
class AffiliateOptInResult:
    participant: AffiliateParticipant
    mode: str
    code: str


def _now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(*parts: str) -> str:
    material = "\x1f".join(part.strip().lower() for part in parts if part)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class AffiliateService:
    CUSTOMER_ELIGIBLE_STATUSES = frozenset({"trialing", "active"})

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _professional_documents(self, professional_id: UUID) -> set[str]:
        professional = await self.db.get(Professional, professional_id)
        subscription_documents = (
            await self.db.execute(
                select(Subscription.billing_document).where(
                    Subscription.professional_id == professional_id,
                    Subscription.billing_document != "",
                )
            )
        ).scalars().all()
        values = [
            professional.cpf if professional else "",
            professional.billing_cnpj if professional else "",
            *subscription_documents,
        ]
        return {normalized for value in values if (normalized := _digits(value))}

    async def _reject_document_self_referral(
        self,
        *,
        referral: AffiliateReferral,
        participant: AffiliateParticipant,
    ) -> bool:
        if participant.professional_id is None:
            return False
        referrer_documents = await self._professional_documents(participant.professional_id)
        referred_documents = await self._professional_documents(
            referral.referred_professional_id
        )
        if not referrer_documents.intersection(referred_documents):
            return False
        referral.status = "rejected"
        referral.review_state = "rejected"
        referral.review_reason = "Autoindicação por documento de cobrança"
        await self.db.flush()
        return True

    async def _active_policy(self, mode: str) -> AffiliatePolicy:
        policy = (
            await self.db.execute(
                select(AffiliatePolicy)
                .where(AffiliatePolicy.mode == mode, AffiliatePolicy.status == "active")
                .order_by(AffiliatePolicy.version.desc())
            )
        ).scalars().first()
        if policy is None:
            raise AffiliateForbiddenError("Programa de indicação indisponível")
        return policy

    async def _new_code(self) -> str:
        for _ in range(10):
            code = secrets.token_urlsafe(12).replace("-", "").replace("_", "").lower()[:18]
            exists = await self.db.scalar(
                select(func.count()).select_from(AffiliateCode).where(AffiliateCode.code == code)
            )
            if not exists:
                return code
        raise AffiliateConflictError("Não foi possível gerar um código único")

    async def _participant_for_professional(
        self, professional: Professional
    ) -> AffiliateParticipant | None:
        return (
            await self.db.execute(
                select(AffiliateParticipant).where(
                    (AffiliateParticipant.professional_id == professional.id)
                    | (AffiliateParticipant.email == professional.email.lower())
                )
            )
        ).scalars().first()

    async def opt_in_customer(
        self, *, professional: Professional, terms_version: str
    ) -> AffiliateOptInResult:
        if professional.is_staff:
            raise AffiliateForbiddenError("Contas da equipe não participam do programa")
        if professional.email_verified_at is None:
            raise AffiliateForbiddenError("Confirme seu e-mail antes de participar")
        if professional.subscription_status not in self.CUSTOMER_ELIGIBLE_STATUSES:
            raise AffiliateForbiddenError("É necessário ter uma assinatura elegível")
        policy = await self._active_policy("customer")
        if terms_version != policy.terms_version:
            raise AffiliateConflictError("Aceite a versão atual dos termos de indicação")

        participant = await self._participant_for_professional(professional)
        if participant is None:
            participant = AffiliateParticipant(
                professional_id=professional.id,
                email=professional.email.lower(),
                public_name=None,
                status="active",
            )
            self.db.add(participant)
            await self.db.flush()
        if participant.status in {"suspended", "deactivated", "closed"}:
            raise AffiliateForbiddenError("Sua participação no programa não está ativa")

        participant.status = "active"
        participant.customer_enabled = True
        participant.customer_terms_version = terms_version
        participant.customer_terms_accepted_at = _now()
        code = (
            await self.db.execute(
                select(AffiliateCode).where(
                    AffiliateCode.participant_id == participant.id,
                    AffiliateCode.mode == "customer",
                    AffiliateCode.status == "active",
                )
            )
        ).scalars().first()
        if code is None:
            code = AffiliateCode(
                participant_id=participant.id,
                mode="customer",
                code=await self._new_code(),
                status="active",
                terms_version=terms_version,
            )
            self.db.add(code)
            await self.db.flush()
        elif code.terms_version != terms_version:
            code.terms_version = terms_version
        return AffiliateOptInResult(participant=participant, mode="customer", code=code.code)

    async def invite_partner(
        self,
        *,
        email: str,
        public_name: str,
        commission_override_bps: int | None = None,
    ) -> AffiliateParticipant:
        normalized_email = email.strip().lower()
        if commission_override_bps is not None and not 0 <= commission_override_bps <= 10000:
            raise AffiliateConflictError("Percentual de comissão inválido")
        participant = (
            await self.db.execute(
                select(AffiliateParticipant).where(AffiliateParticipant.email == normalized_email)
            )
        ).scalar_one_or_none()
        if participant is None:
            participant = AffiliateParticipant(
                email=normalized_email,
                public_name=public_name.strip(),
                status="invited",
                partner_enabled=True,
                commission_override_bps=commission_override_bps,
            )
            self.db.add(participant)
            await self.db.flush()
        else:
            participant.partner_enabled = True
            participant.public_name = public_name.strip()
            participant.commission_override_bps = commission_override_bps
        return participant

    async def activate_partner(
        self, *, participant: AffiliateParticipant, terms_version: str
    ) -> AffiliateOptInResult:
        policy = await self._active_policy("partner")
        if terms_version != policy.terms_version:
            raise AffiliateConflictError("Aceite a versão atual dos termos de afiliado")
        if participant.status in {"suspended", "deactivated", "closed"}:
            raise AffiliateForbiddenError("Sua participação no programa não está ativa")
        participant.status = "active"
        participant.partner_enabled = True
        participant.partner_terms_version = terms_version
        participant.partner_terms_accepted_at = _now()
        code = (
            await self.db.execute(
                select(AffiliateCode).where(
                    AffiliateCode.participant_id == participant.id,
                    AffiliateCode.mode == "partner",
                    AffiliateCode.status == "active",
                )
            )
        ).scalars().first()
        if code is None:
            code = AffiliateCode(
                participant_id=participant.id,
                mode="partner",
                code=await self._new_code(),
                status="active",
                terms_version=terms_version,
            )
            self.db.add(code)
            await self.db.flush()
        elif code.terms_version != terms_version:
            code.terms_version = terms_version
        return AffiliateOptInResult(participant=participant, mode="partner", code=code.code)

    async def resolve_public_code(self, code: str) -> dict:
        code_row = (
            await self.db.execute(
                select(AffiliateCode).where(
                    AffiliateCode.code == code.strip().lower(),
                    AffiliateCode.status == "active",
                )
            )
        ).scalar_one_or_none()
        if code_row is None:
            raise AffiliateNotFoundError("Código de indicação inválido")
        participant = await self.db.get(AffiliateParticipant, code_row.participant_id)
        if participant is None or participant.status != "active":
            raise AffiliateNotFoundError("Código de indicação indisponível")
        policy = await self._active_policy(code_row.mode)
        accepted_terms = (
            participant.customer_terms_version
            if code_row.mode == "customer"
            else participant.partner_terms_version
        )
        if (
            code_row.terms_version != policy.terms_version
            or accepted_terms != policy.terms_version
        ):
            raise AffiliateNotFoundError("Código de indicação indisponível")
        return {
            "code": code_row.code,
            "mode": code_row.mode,
            "benefitPercent": policy.referral_discount_bps // 100,
            "publicName": participant.public_name if code_row.mode == "partner" else None,
            "expiresInDays": policy.attribution_window_days,
        }

    async def register_referral(
        self,
        *,
        code: str,
        referred_professional: Professional,
        request_ip: str,
        user_agent: str,
    ) -> AffiliateReferral:
        existing = (
            await self.db.execute(
                select(AffiliateReferral).where(
                    AffiliateReferral.referred_professional_id == referred_professional.id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AffiliateConflictError("Esta conta já foi atribuída a uma indicação")

        code_row = (
            await self.db.execute(
                select(AffiliateCode).where(
                    AffiliateCode.code == code.strip().lower(), AffiliateCode.status == "active"
                )
            )
        ).scalar_one_or_none()
        if code_row is None:
            raise AffiliateNotFoundError("Código de indicação inválido")
        participant = await self.db.get(AffiliateParticipant, code_row.participant_id)
        if participant is None or participant.status != "active":
            raise AffiliateForbiddenError("Participante não está ativo")
        if code_row.mode == "customer" and not participant.customer_enabled:
            raise AffiliateForbiddenError("Código de cliente indisponível")
        if code_row.mode == "partner" and not participant.partner_enabled:
            raise AffiliateForbiddenError("Código de parceiro indisponível")
        if participant.professional_id == referred_professional.id:
            raise AffiliateForbiddenError("Autoindicação não é permitida")
        referrer = (
            await self.db.get(Professional, participant.professional_id)
            if participant.professional_id
            else None
        )
        referrer_document = _digits(referrer.cpf or referrer.billing_cnpj) if referrer else ""
        referred_document = _digits(
            referred_professional.cpf or referred_professional.billing_cnpj
        )
        if referrer_document and referred_document and referrer_document == referred_document:
            raise AffiliateForbiddenError("Autoindicação não é permitida")

        policy = await self._active_policy(code_row.mode)
        accepted_terms = (
            participant.customer_terms_version
            if code_row.mode == "customer"
            else participant.partner_terms_version
        )
        if (
            code_row.terms_version != policy.terms_version
            or accepted_terms != policy.terms_version
        ):
            raise AffiliateForbiddenError("Aceite a versão atual dos termos do programa")
        effective_commission = participant.commission_override_bps
        snapshot = policy.snapshot(commission_bps=effective_commission)
        fingerprint = _fingerprint(request_ip, user_agent)
        review_state = "clear"
        review_reason = None
        reused_source = await self.db.scalar(
            select(func.count())
            .select_from(AffiliateReferral)
            .where(AffiliateReferral.source_fingerprint == fingerprint)
        )
        if reused_source:
            review_state = "manual_review"
            review_reason = "Dispositivo ou rede coincidente"
        elif referrer and referrer.phone and referrer.phone == referred_professional.phone:
            review_state = "manual_review"
            review_reason = "Telefone coincidente"
        referral = AffiliateReferral(
            participant_id=participant.id,
            code_id=code_row.id,
            referred_professional_id=referred_professional.id,
            policy_id=policy.id,
            mode=code_row.mode,
            status="registered",
            review_state=review_state,
            review_reason=review_reason,
            policy_snapshot=snapshot,
            source_fingerprint=fingerprint,
            benefit_expires_at=_now() + timedelta(days=policy.attribution_window_days),
        )
        self.db.add(referral)
        await self.db.flush()
        await AffiliateNotificationService(self.db).notify(
            participant=participant,
            event_type="affiliate_referral",
            title="Nova indicação registrada",
            body="Uma nova conta foi atribuída ao seu código. A identidade permanece protegida.",
        )
        return referral

    async def referral_discount(self, professional_id: UUID) -> tuple[int, AffiliateReferral | None]:
        referral = (
            await self.db.execute(
                select(AffiliateReferral).where(
                    AffiliateReferral.referred_professional_id == professional_id,
                    AffiliateReferral.status != "rejected",
                )
            )
        ).scalar_one_or_none()
        if (
            referral is None
            or referral.status != "registered"
            or referral.benefit_expires_at < _now()
        ):
            return 0, referral
        participant = await self.db.get(AffiliateParticipant, referral.participant_id)
        if participant is None or await self._reject_document_self_referral(
            referral=referral,
            participant=participant,
        ):
            return 0, referral
        return int(referral.policy_snapshot.get("referralDiscountBps", 0)), referral

    async def _add_ledger(
        self,
        *,
        participant_id: UUID,
        reward_id: UUID | None,
        entry_type: str,
        account: str,
        amount_cents: int,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> AffiliateLedgerEntry:
        existing = (
            await self.db.execute(
                select(AffiliateLedgerEntry).where(
                    AffiliateLedgerEntry.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        entry = AffiliateLedgerEntry(
            participant_id=participant_id,
            reward_id=reward_id,
            entry_type=entry_type,
            account=account,
            amount_cents=amount_cents,
            idempotency_key=idempotency_key,
            metadata_json=metadata,
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def record_external_payment(
        self,
        *,
        referred_professional_id: UUID,
        external_payment_id: str,
        external_event_id: str,
        provider_event: str,
        received_revenue_cents: int,
        plan_interval: str,
        occurred_at: datetime,
    ) -> AffiliateReward | None:
        if received_revenue_cents <= 0:
            return None
        referral = (
            await self.db.execute(
                select(AffiliateReferral).where(
                    AffiliateReferral.referred_professional_id == referred_professional_id,
                    AffiliateReferral.status != "rejected",
                )
            )
        ).scalar_one_or_none()
        if referral is None or referral.review_state in {"manual_review", "rejected"}:
            return None
        participant = await self.db.get(AffiliateParticipant, referral.participant_id)
        if participant is None or participant.status != "active":
            return None
        if await self._reject_document_self_referral(
            referral=referral,
            participant=participant,
        ):
            return None
        kind = "partner_recurring" if referral.mode == "partner" else "customer_once"
        if kind == "customer_once":
            prior = (
                await self.db.execute(
                    select(AffiliateReward).where(
                        AffiliateReward.referral_id == referral.id,
                        AffiliateReward.kind == kind,
                    )
                )
            ).scalars().first()
            if prior is not None and prior.source_payment_id != external_payment_id:
                return None
        reward = (
            await self.db.execute(
                select(AffiliateReward).where(
                    AffiliateReward.referral_id == referral.id,
                    AffiliateReward.source_payment_id == external_payment_id,
                    AffiliateReward.kind == kind,
                )
            )
        ).scalar_one_or_none()
        if reward is None:
            if referral.mode == "partner":
                bps = int(referral.policy_snapshot.get("commissionBps", 0))
                gross_cents = received_revenue_cents * bps // 10000
            else:
                interval_key = str(plan_interval).lower()
                rewards = {
                    "monthly": "customerRewardMonthlyCents",
                    "quarterly": "customerRewardQuarterlyCents",
                    "yearly": "customerRewardYearlyCents",
                    "annual": "customerRewardYearlyCents",
                }
                gross_cents = int(referral.policy_snapshot.get(rewards.get(interval_key, ""), 0))
            if gross_cents <= 0:
                return None
            reward = AffiliateReward(
                participant_id=participant.id,
                referral_id=referral.id,
                source_payment_id=external_payment_id,
                source_event_id=external_event_id,
                kind=kind,
                state="pending",
                gross_cents=gross_cents,
                external_revenue_cents=received_revenue_cents,
            )
            self.db.add(reward)
            await self.db.flush()
            await self._add_ledger(
                participant_id=participant.id,
                reward_id=reward.id,
                entry_type="reward_pending",
                account="pending",
                amount_cents=gross_cents,
                idempotency_key=f"reward:{external_payment_id}:pending",
                metadata={"mode": referral.mode},
            )
            await AffiliateNotificationService(self.db).notify(
                participant=participant,
                event_type="affiliate_convert",
                title="Sua indicação converteu",
                body="O pagamento foi confirmado e a recompensa entrou no período de segurança.",
            )
        if provider_event == "PAYMENT_RECEIVED" and reward.state == "pending":
            reward.state = "coolingOff"
            reward.available_at = occurred_at + timedelta(
                days=int(referral.policy_snapshot.get("coolingOffDays", 14))
            )
            reward.source_event_id = external_event_id
        if referral.status == "registered":
            referral.status = "converted"
            referral.converted_at = occurred_at
        await self.db.flush()
        return reward

    async def reverse_external_payment(
        self,
        *,
        external_payment_id: str,
        external_event_id: str,
        reversed_revenue_cents: int | None = None,
    ) -> AffiliateReward | None:
        reward = (
            await self.db.execute(
                select(AffiliateReward).where(
                    AffiliateReward.source_payment_id == external_payment_id
                )
            )
        ).scalars().first()
        if reward is None or reward.state in {"reversed", "voided"}:
            return reward
        if reversed_revenue_cents is None or reversed_revenue_cents >= reward.external_revenue_cents:
            reverse_cents = reward.gross_cents - reward.reversed_cents
        else:
            proportional_total = (
                reward.gross_cents * max(0, reversed_revenue_cents) // reward.external_revenue_cents
            )
            reverse_cents = max(0, proportional_total - reward.reversed_cents)
        if reverse_cents <= 0:
            return reward
        if reward.state in {"pending", "coolingOff"}:
            account = "pending"
        else:
            # Paid/credited reversals deliberately create a negative available balance.
            account = "available"
        await self._add_ledger(
            participant_id=reward.participant_id,
            reward_id=reward.id,
            entry_type="reward_reversal",
            account=account,
            amount_cents=-reverse_cents,
            idempotency_key=f"reward-reversal:{external_event_id}",
            metadata={"sourcePaymentId": external_payment_id},
        )
        reward.reversed_cents += reverse_cents
        if reward.reversed_cents >= reward.gross_cents:
            reward.state = "reversed"
        participant = await self.db.get(AffiliateParticipant, reward.participant_id)
        if participant is not None:
            await AffiliateNotificationService(self.db).notify(
                participant=participant,
                event_type="affiliate_reverse",
                title="Reversão de recompensa",
                body="Um reembolso ou chargeback gerou reversão proporcional no saldo.",
                severity="warning",
            )
        await self.db.flush()
        return reward

    async def release_due_rewards(self, *, now: datetime | None = None) -> int:
        cutoff = now or _now()
        rewards = (
            await self.db.execute(
                select(AffiliateReward).where(
                    AffiliateReward.state == "coolingOff",
                    AffiliateReward.available_at <= cutoff,
                )
            )
        ).scalars().all()
        released = 0
        for reward in rewards:
            participant = await self.db.get(AffiliateParticipant, reward.participant_id)
            if participant is None or participant.status != "active":
                continue
            net_cents = max(0, reward.gross_cents - reward.reversed_cents)
            if net_cents == 0:
                reward.state = "reversed"
                continue
            reward.state = "available"
            await self._add_ledger(
                participant_id=reward.participant_id,
                reward_id=reward.id,
                entry_type="reward_pending_released",
                account="pending",
                amount_cents=-net_cents,
                idempotency_key=f"reward:{reward.id}:pending-release",
            )
            await self._add_ledger(
                participant_id=reward.participant_id,
                reward_id=reward.id,
                entry_type="reward_available",
                account="available",
                amount_cents=net_cents,
                idempotency_key=f"reward:{reward.id}:available",
            )
            await AffiliateNotificationService(self.db).notify(
                participant=participant,
                event_type="affiliate_reward",
                title="Recompensa disponível",
                body=f"R$ {net_cents / 100:.2f} estão disponíveis para resgate.",
            )
            released += 1
        await self.db.flush()
        return released

    async def balances(self, participant_id: UUID) -> dict[str, int]:
        rows = (
            await self.db.execute(
                select(
                    AffiliateLedgerEntry.account,
                    func.coalesce(func.sum(AffiliateLedgerEntry.amount_cents), 0),
                )
                .where(AffiliateLedgerEntry.participant_id == participant_id)
                .group_by(AffiliateLedgerEntry.account)
            )
        ).all()
        balances = {"pending": 0, "available": 0, "reserved": 0, "credit": 0, "cash": 0}
        balances.update({account: int(amount) for account, amount in rows})
        return balances

    async def customer_dashboard(self, professional: Professional) -> dict:
        participant = await self._participant_for_professional(professional)
        active_policy = await self._active_policy("customer")
        if participant is None:
            return {
                "eligible": professional.subscription_status in self.CUSTOMER_ELIGIBLE_STATUSES
                and professional.email_verified_at is not None
                and not professional.is_staff,
                "termsVersion": active_policy.terms_version,
                "participant": None,
                "code": None,
                "balances": {"pending": 0, "available": 0, "reserved": 0, "credit": 0, "cash": 0},
                "referrals": [],
                "rewards": [],
            }
        code = (
            await self.db.execute(
                select(AffiliateCode).where(
                    AffiliateCode.participant_id == participant.id,
                    AffiliateCode.mode == "customer",
                    AffiliateCode.status == "active",
                )
            )
        ).scalars().first()
        referrals = (
            await self.db.execute(
                select(AffiliateReferral)
                .where(AffiliateReferral.participant_id == participant.id)
                .order_by(AffiliateReferral.created_at.desc())
            )
        ).scalars().all()
        rewards = (
            await self.db.execute(
                select(AffiliateReward)
                .where(AffiliateReward.participant_id == participant.id)
                .order_by(AffiliateReward.created_at.desc())
            )
        ).scalars().all()
        return {
            "eligible": professional.subscription_status in self.CUSTOMER_ELIGIBLE_STATUSES,
            "termsVersion": active_policy.terms_version,
            "participant": participant,
            "code": code.code if code else None,
            "balances": await self.balances(participant.id),
            "referrals": referrals,
            "rewards": rewards,
        }

    async def admin_overview(self) -> dict:
        async def count(model, *filters) -> int:
            stmt = select(func.count()).select_from(model)
            if filters:
                stmt = stmt.where(*filters)
            return int((await self.db.execute(stmt)).scalar_one())

        available = await self.db.scalar(
            select(func.coalesce(func.sum(AffiliateLedgerEntry.amount_cents), 0)).where(
                AffiliateLedgerEntry.account == "available"
            )
        )
        return {
            "activePolicies": await count(AffiliatePolicy, AffiliatePolicy.status == "active"),
            "participants": await count(AffiliateParticipant),
            "activeParticipants": await count(
                AffiliateParticipant, AffiliateParticipant.status == "active"
            ),
            "referrals": await count(AffiliateReferral),
            "pendingReviews": await count(
                AffiliateReferral, AffiliateReferral.review_state == "manual_review"
            ),
            "rewards": await count(AffiliateReward),
            "availableCents": int(available or 0),
        }

    async def list_participants(self, *, query: str | None = None) -> list[dict]:
        stmt = select(AffiliateParticipant).order_by(AffiliateParticipant.created_at.desc())
        if query:
            pattern = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                func.lower(AffiliateParticipant.email).like(pattern)
                | func.lower(func.coalesce(AffiliateParticipant.public_name, "")).like(pattern)
            )
        rows = (await self.db.execute(stmt)).scalars().all()
        result = []
        for row in rows:
            result.append(
                {
                    "id": row.id,
                    "email": row.email,
                    "publicName": row.public_name,
                    "status": row.status,
                    "customerEnabled": row.customer_enabled,
                    "partnerEnabled": row.partner_enabled,
                    "commissionOverrideBps": row.commission_override_bps,
                    "balances": await self.balances(row.id),
                    "createdAt": row.created_at,
                }
            )
        return result

    async def create_policy(self, *, actor: Professional, values: dict, activate: bool) -> AffiliatePolicy:
        mode = values["mode"]
        latest = await self.db.scalar(
            select(func.coalesce(func.max(AffiliatePolicy.version), 0)).where(
                AffiliatePolicy.mode == mode
            )
        )
        if activate:
            current = (
                await self.db.execute(
                    select(AffiliatePolicy).where(
                        AffiliatePolicy.mode == mode, AffiliatePolicy.status == "active"
                    )
                )
            ).scalars().all()
            for policy in current:
                policy.status = "retired"
        policy = AffiliatePolicy(
            mode=mode,
            version=int(latest or 0) + 1,
            status="active" if activate else "draft",
            terms_version=values["terms_version"],
            referral_discount_bps=values["referral_discount_bps"],
            commission_bps=values["commission_bps"],
            customer_reward_monthly_cents=values["customer_reward_monthly_cents"],
            customer_reward_quarterly_cents=values["customer_reward_quarterly_cents"],
            customer_reward_yearly_cents=values["customer_reward_yearly_cents"],
            attribution_window_days=values.get("attribution_window_days", 30),
            cooling_off_days=values.get("cooling_off_days", 14),
            payout_minimum_cents=values.get("payout_minimum_cents", 10000),
            effective_at=values["effective_at"],
            created_by_id=actor.id,
        )
        self.db.add(policy)
        await self.db.flush()
        return policy

    async def review_referral(
        self,
        *,
        referral_id: UUID,
        decision: str,
        reason: str,
    ) -> AffiliateReferral:
        referral = await self.db.get(AffiliateReferral, referral_id)
        if referral is None:
            raise AffiliateNotFoundError("Indicação não encontrada")
        referral.review_state = decision
        referral.review_reason = reason.strip()
        if decision == "rejected":
            referral.status = "rejected"
            rewards = (
                await self.db.execute(
                    select(AffiliateReward).where(
                        AffiliateReward.referral_id == referral.id,
                        AffiliateReward.state.in_(["pending", "coolingOff"]),
                    )
                )
            ).scalars().all()
            for reward in rewards:
                pending_cents = int(
                    await self.db.scalar(
                        select(
                            func.coalesce(func.sum(AffiliateLedgerEntry.amount_cents), 0)
                        ).where(
                            AffiliateLedgerEntry.reward_id == reward.id,
                            AffiliateLedgerEntry.account == "pending",
                        )
                    )
                    or 0
                )
                reward.state = "voided"
                if pending_cents <= 0:
                    continue
                await self._add_ledger(
                    participant_id=reward.participant_id,
                    reward_id=reward.id,
                    entry_type="reward_voided",
                    account="pending",
                    amount_cents=-pending_cents,
                    idempotency_key=f"reward:{reward.id}:risk-rejected",
                    metadata={"reason": reason.strip()},
                )
        await self.db.flush()
        return referral

    async def set_participant_status(
        self, *, participant_id: UUID, status: str, reason: str
    ) -> AffiliateParticipant:
        if status not in {"active", "suspended", "deactivated", "closed"}:
            raise AffiliateConflictError("Estado de participante inválido")
        participant = await self.db.get(AffiliateParticipant, participant_id)
        if participant is None:
            raise AffiliateNotFoundError("Participante não encontrado")
        participant.status = status
        participant.suspension_reason = reason.strip() or None
        if status in {"deactivated", "closed"}:
            participant.deactivated_at = _now()
            pending = (
                await self.db.execute(
                    select(AffiliateReward).where(
                        AffiliateReward.participant_id == participant.id,
                        AffiliateReward.state.in_(["pending", "coolingOff"]),
                    )
                )
            ).scalars().all()
            for reward in pending:
                net_cents = max(0, reward.gross_cents - reward.reversed_cents)
                reward.state = "voided"
                if net_cents == 0:
                    continue
                await self._add_ledger(
                    participant_id=participant.id,
                    reward_id=reward.id,
                    entry_type="reward_voided",
                    account="pending",
                    amount_cents=-net_cents,
                    idempotency_key=f"reward:{reward.id}:voided",
                    metadata={"reason": "participant_deactivated"},
                )
        if status == "suspended":
            await AffiliateNotificationService(self.db).notify(
                participant=participant,
                event_type="affiliate_review",
                title="Participação suspensa para revisão",
                body="Novas indicações, resgates e pagamentos ficam congelados durante a revisão.",
                severity="warning",
            )
        await self.db.flush()
        return participant
