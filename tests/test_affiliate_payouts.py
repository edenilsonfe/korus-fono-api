"""Fiscal data and dual-control affiliate payout contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.core.security import hash_password
from app.models.affiliate import (
    AffiliateCode,
    AffiliateLedgerEntry,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
)
from app.models.professional import Professional
from app.services.affiliate_payout_service import (
    AffiliatePayoutConflictError,
    AffiliatePayoutForbiddenError,
    AffiliatePayoutService,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def funded_partner(db_session, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key", lambda: key
    )
    participant = AffiliateParticipant(
        email="funded@example.com", status="active", partner_enabled=True
    )
    db_session.add(participant)
    await db_session.flush()
    service = AffiliatePayoutService(db_session)
    profile = await service.submit_fiscal_profile(
        participant=participant,
        person_type="pf",
        legal_name="Pessoa Teste",
        document="52998224725",
        pix_key_type="cpf",
        pix_key="52998224725",
    )
    profile.status = "approved"
    profile.pix_validated_at = datetime.now(UTC)
    profile.withdrawal_locked_until = datetime.now(UTC) - timedelta(days=1)
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test",
            account="available",
            amount_cents=30000,
            idempotency_key="funded-test",
        )
    )
    await db_session.commit()
    return participant, profile, service


async def test_payout_idempotency_and_changed_amount(db_session, funded_partner):
    participant, _, service = funded_partner
    first = await service.request_cash_payout(
        participant=participant,
        amount_cents=10000,
        cash_enabled=True,
        request_id="unique-request",
    )
    same = await service.request_cash_payout(
        participant=participant,
        amount_cents=10000,
        cash_enabled=True,
        request_id="unique-request",
    )
    assert same.id == first.id
    assert await service._available(participant.id) == 20000
    with pytest.raises(AffiliatePayoutConflictError, match="outro valor"):
        await service.request_cash_payout(
            participant=participant,
            amount_cents=15000,
            cash_enabled=True,
            request_id="unique-request",
        )


async def test_transfer_done_before_link_is_verified_and_settled_once(
    db_session, funded_partner, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.core.config import get_settings
    from app.services.affiliate_service import AffiliateService

    monkeypatch.setattr(get_settings(), "asaas_api_key", "test-key-no-network")
    participant, _, service = funded_partner
    payout = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    payout.status = "approved"
    await db_session.commit()
    assert (
        await service.complete_transfer(
            provider_transfer_id="transfer-early", succeeded=True
        )
        is None
    )
    transfer = {
        "id": "transfer-early",
        "value": 100,
        "bankAccount": {"pixAddressKey": "52998224725"},
        "externalReference": str(payout.id),
        "status": "DONE",
    }
    monkeypatch.setattr(
        "app.billing.asaas_gateway.AsaasPaymentGateway.get_transfer",
        AsyncMock(return_value=transfer),
    )
    participant.status = (
        "suspended"  # Provider fact must still be accounted after a later hold.
    )
    await db_session.flush()
    for _ in range(2):
        result = await service.reconcile_transfer(
            payout_id=payout.id, provider_transfer_id="transfer-early"
        )
        assert result.status == "paid"
    assert (await AffiliateService(db_session).balances(participant.id))[
        "reserved"
    ] == 0
    for field, value in [
        ("value", 99),
        ("bankAccount", {"pixAddressKey": "11144477735"}),
        ("externalReference", "other"),
    ]:
        with pytest.raises(AffiliatePayoutConflictError):
            await service.verify_transfer(
                payout_id=payout.id, transfer={**transfer, field: value}
            )


async def test_obsolete_fiscal_profile_cannot_be_reapproved_or_paid(
    db_session, funded_partner
):
    participant, old, service = funded_partner
    payout = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    payout.status = "approved"
    await service.submit_fiscal_profile(
        participant=participant,
        person_type="pf",
        legal_name="New profile",
        document="52998224725",
        pix_key_type="cpf",
        pix_key="52998224725",
    )
    actor = await _admin(db_session, "obsolete-approver@example.com")
    with pytest.raises(AffiliatePayoutConflictError, match="vigente"):
        await service.approve_fiscal_profile(
            profile_id=old.id, actor=actor, pix_validated=True
        )
    with pytest.raises(AffiliatePayoutConflictError):
        await service.mark_transfer_processing(
            payout_id=payout.id, provider_transfer_id="blocked-transfer"
        )


async def test_invalid_document_check_digits_are_rejected(db_session, funded_partner):
    participant, _, service = funded_partner
    with pytest.raises(AffiliatePayoutConflictError, match="Documento"):
        await service.submit_fiscal_profile(
            participant=participant,
            person_type="pf",
            legal_name="Test",
            document="11111111111",
            pix_key_type="cpf",
            pix_key="52998224725",
        )


async def test_admin_can_cancel_approved_reserve_but_not_a_sent_transfer(
    db_session, funded_partner
):
    participant, _, service = funded_partner
    payout = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    payout.status = "approved"
    await db_session.flush()
    canceled = await service.cancel_payout(
        payout_id=payout.id, participant_id=participant.id, admin_override=True
    )
    assert canceled.status == "canceled"
    assert await service._available(participant.id) == 30000
    sent = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    sent.status = "processing"
    sent.provider_transfer_id = "transfer-sent"
    await db_session.flush()
    with pytest.raises(AffiliatePayoutConflictError):
        await service.cancel_payout(
            payout_id=sent.id, participant_id=participant.id, admin_override=True
        )


async def test_failed_transfer_returns_reserve_once_and_worker_reconciles(
    db_session, funded_partner, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.core.config import get_settings
    from app.services.billing_event_recovery import reconcile_pending_transfers

    participant, _, service = funded_partner
    payout = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    payout.status = "approved"
    await db_session.flush()
    await service.mark_transfer_processing(
        payout_id=payout.id, provider_transfer_id="transfer-failed"
    )
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "asaas_api_key", "test-no-network")
    monkeypatch.setattr(
        "app.billing.asaas_gateway.AsaasPaymentGateway.get_transfer",
        AsyncMock(
            return_value={
                "id": "transfer-failed",
                "pixAddressKey": "52998224725",
                "value": 100,
                "externalReference": str(payout.id),
                "status": "FAILED",
            }
        ),
    )
    assert await reconcile_pending_transfers(db_session) == 1
    assert await reconcile_pending_transfers(db_session) == 0
    assert await service._available(participant.id) == 30000


async def _admin(db, email: str) -> Professional:
    row = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=email,
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
        is_staff=True,
        admin_role="billing",
    )
    db.add(row)
    await db.flush()
    return row


async def test_fiscal_profile_encrypts_document_and_pix_and_locks_changes(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key",
        lambda: Fernet.generate_key().decode(),
    )
    participant = AffiliateParticipant(
        email="fiscal-partner@example.com",
        public_name="Fiscal partner",
        status="active",
        partner_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()

    profile = await AffiliatePayoutService(db_session).submit_fiscal_profile(
        participant=participant,
        person_type="pf",
        legal_name="Pessoa Afiliada",
        document="52998224725",
        pix_key_type="cpf",
        pix_key="52998224725",
    )
    await db_session.commit()

    assert profile.document_masked.endswith("4725")
    assert profile.pix_key_masked.endswith("4725")
    assert profile.encrypted_document != "52998224725"
    assert profile.encrypted_pix_key != "52998224725"
    assert profile.withdrawal_locked_until >= datetime.now(UTC) + timedelta(hours=47)


async def test_fiscal_profile_blocks_document_or_pix_key_owned_by_referred_account(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key",
        lambda: Fernet.generate_key().decode(),
    )
    participant = AffiliateParticipant(
        email="anti-self-partner@example.com",
        status="active",
        partner_enabled=True,
    )
    policy = AffiliatePolicy(
        mode="partner",
        version=1,
        status="active",
        terms_version="partner-v1",
        referral_discount_bps=1500,
        commission_bps=2000,
        effective_at=datetime.now(UTC),
    )
    referred = Professional(
        email="anti-self-buyer@example.com",
        password_hash=hash_password("testpass123"),
        name="Comprador indicado",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        cpf="52998224725",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add_all([participant, policy, referred])
    await db_session.flush()
    code = AffiliateCode(
        participant_id=participant.id,
        mode="partner",
        code="antiselfpartner123",
        status="active",
        terms_version=policy.terms_version,
    )
    db_session.add(code)
    await db_session.flush()
    db_session.add(
        AffiliateReferral(
            participant_id=participant.id,
            code_id=code.id,
            referred_professional_id=referred.id,
            policy_id=policy.id,
            mode="partner",
            status="registered",
            policy_snapshot=policy.snapshot(),
            benefit_expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    await db_session.flush()

    with pytest.raises(AffiliatePayoutForbiddenError, match="Autoindicação"):
        await AffiliatePayoutService(db_session).submit_fiscal_profile(
            participant=participant,
            person_type="pf",
            legal_name="Mesmo titular",
            document="529.982.247-25",
            pix_key_type="cpf",
            pix_key="52998224725",
        )


async def test_cash_request_requires_approved_profile_and_minimum_balance(
    db_session, monkeypatch
):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key", lambda: key
    )
    participant = AffiliateParticipant(
        email="cash-partner@example.com",
        status="active",
        partner_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    service = AffiliatePayoutService(db_session)
    profile = await service.submit_fiscal_profile(
        participant=participant,
        person_type="pj",
        legal_name="Afiliado LTDA",
        document="11222333000181",
        pix_key_type="cnpj",
        pix_key="11222333000181",
    )
    profile.status = "approved"
    profile.pix_validated_at = datetime.now(UTC)
    profile.withdrawal_locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_available",
            account="available",
            amount_cents=9900,
            idempotency_key="test:available:below-minimum",
        )
    )
    await db_session.commit()

    with pytest.raises(AffiliatePayoutForbiddenError, match=r"R\$ 100"):
        await service.request_cash_payout(
            participant=participant,
            amount_cents=9900,
            cash_enabled=True,
        )

    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_available_more",
            account="available",
            amount_cents=5100,
            idempotency_key="test:available:more",
        )
    )
    await db_session.flush()
    payout = await service.request_cash_payout(
        participant=participant,
        amount_cents=15000,
        cash_enabled=True,
    )
    assert payout.status == "requested"
    assert payout.gross_cents == 15000
    assert payout.net_cents == 15000


async def test_batch_requires_a_different_approver(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key",
        lambda: Fernet.generate_key().decode(),
    )
    preparer = await _admin(db_session, "preparer@example.com")
    approver = await _admin(db_session, "approver@example.com")
    participant = AffiliateParticipant(
        email="batch-partner@example.com",
        status="active",
        partner_enabled=True,
    )
    db_session.add(participant)
    await db_session.flush()
    service = AffiliatePayoutService(db_session)
    profile = await service.submit_fiscal_profile(
        participant=participant,
        person_type="pf",
        legal_name="Parceiro Lote",
        document="52998224725",
        pix_key_type="cpf",
        pix_key="52998224725",
    )
    profile.status = "approved"
    profile.pix_validated_at = datetime.now(UTC)
    profile.withdrawal_locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            entry_type="test_batch_available",
            account="available",
            amount_cents=10000,
            idempotency_key="test:batch:available",
        )
    )
    await db_session.flush()
    payout = await service.request_cash_payout(
        participant=participant, amount_cents=10000, cash_enabled=True
    )
    payout.cancellable_until = datetime.now(UTC) - timedelta(seconds=1)
    batch = await service.create_weekly_batch(actor=preparer, now=datetime.now(UTC))
    assert payout.batch_id == batch.id

    with pytest.raises(AffiliatePayoutConflictError, match="segunda pessoa"):
        await service.approve_batch(
            batch_id=batch.id, actor=preparer, allow_single_operator=False
        )

    approved = await service.approve_batch(
        batch_id=batch.id,
        actor=approver,
        allow_single_operator=False,
    )
    assert approved.status == "approved"

    processing = await service.mark_transfer_processing(
        payout_id=payout.id,
        provider_transfer_id="transfer-batch-done",
    )
    assert processing.status == "processing"
    assert batch.status == "processing"
    completed = await service.complete_transfer(
        provider_transfer_id="transfer-batch-done",
        succeeded=True,
    )
    assert completed is payout
    assert payout.status == "paid"
    assert batch.status == "paid"
