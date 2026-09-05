from datetime import UTC, datetime

import pytest

from app.models.affiliate import AffiliateParticipant
from app.services.affiliate_portal_service import (
    AffiliatePortalForbiddenError,
    AffiliatePortalService,
)

pytestmark = pytest.mark.asyncio


async def test_magic_link_is_one_use_and_portal_session_is_scoped(db_session):
    participant = AffiliateParticipant(
        email="portal-partner@example.com",
        public_name="Parceiro Portal",
        status="active",
        partner_enabled=True,
        partner_terms_version="partner-v1",
        partner_terms_accepted_at=datetime.now(UTC),
    )
    db_session.add(participant)
    await db_session.flush()
    service = AffiliatePortalService(db_session)
    raw_token = await service.create_magic_link(participant)
    await db_session.commit()

    resolved = await service.exchange_magic_link(raw_token)
    assert resolved.id == participant.id
    session_token = service.create_session_token(resolved)
    assert service.decode_session_token(session_token) == participant.id

    with pytest.raises(AffiliatePortalForbiddenError, match="usado"):
        await service.exchange_magic_link(raw_token)


async def test_portal_link_expiry_unknown_email_and_wrong_token_scope(db_session):
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import select

    from app.core.security import create_access_token
    from app.models.affiliate import AffiliateMagicLink

    service = AffiliatePortalService(db_session)
    assert await service.request_magic_link("unknown@example.com") is None
    participant = AffiliateParticipant(
        email="expiry@example.com", status="active", partner_enabled=True
    )
    db_session.add(participant)
    await db_session.flush()
    resolved, token = await service.request_magic_link(participant.email)
    assert resolved.id == participant.id
    row = await db_session.scalar(select(AffiliateMagicLink))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(AffiliatePortalForbiddenError, match="expirado"):
        await service.exchange_magic_link(token)
    with pytest.raises(AffiliatePortalForbiddenError):
        service.decode_session_token(create_access_token(uuid4(), 0))


async def test_portal_http_journey_requires_header_and_scopes_payout(
    api_client, db_session, monkeypatch
):
    from datetime import timedelta
    from uuid import uuid4

    from cryptography.fernet import Fernet

    from app.core.config import get_settings
    from app.models.affiliate import (
        AffiliateFiscalProfile,
        AffiliateLedgerEntry,
        AffiliatePolicy,
    )
    from app.models.feature_flag import FeatureFlag
    from app.services.affiliate_service import AffiliateService

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        "app.services.affiliate_payout_service.get_affiliate_fernet_key", lambda: key
    )
    monkeypatch.setattr(get_settings(), "affiliate_cash_payouts_enabled", True)
    participant = AffiliateParticipant(
        email="journey@example.com", status="invited", partner_enabled=True
    )
    db_session.add_all(
        [
            participant,
            AffiliatePolicy(
                mode="partner",
                version=1,
                status="active",
                terms_version="journey-v1",
                commission_bps=1800,
                payout_minimum_cents=12000,
                effective_at=datetime.now(UTC),
            ),
            FeatureFlag(key="affiliate_partner_program", enabled_global=True),
            FeatureFlag(key="affiliate_cash_payouts", enabled_global=True),
        ]
    )
    await db_session.flush()
    raw = await AffiliatePortalService(db_session).create_magic_link(participant)
    await db_session.commit()
    root = "/api/v1/affiliate-portal"
    assert (await api_client.get(root + "/me")).status_code == 401
    response = await api_client.post(root + "/exchange", json={"token": raw})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert (
        await api_client.post(root + "/exchange", json={"token": raw})
    ).status_code == 400
    assert (
        await api_client.post(
            root + "/accept-terms", json={"termsVersion": "journey-v1"}
        )
    ).status_code == 403
    headers = {"x-affiliate-portal": "1"}
    assert (
        await api_client.post(
            root + "/accept-terms", json={"termsVersion": "journey-v1"}, headers=headers
        )
    ).status_code == 200
    dashboard = (await api_client.get(root + "/dashboard")).json()
    assert dashboard["commissionBps"] == 1800
    assert dashboard["payoutMinimumCents"] == 12000
    profile_response = await api_client.post(
        root + "/fiscal-profiles",
        headers=headers,
        json={
            "personType": "pf",
            "legalName": "Journey",
            "document": "52998224725",
            "pixKeyType": "cpf",
            "pixKey": "52998224725",
        },
    )
    assert profile_response.status_code == 200
    assert "encrypted" not in profile_response.text.lower()
    from uuid import UUID

    profile = await db_session.get(
        AffiliateFiscalProfile, UUID(profile_response.json()["id"])
    )
    profile.status = "approved"
    profile.pix_validated_at = datetime.now(UTC)
    profile.withdrawal_locked_until = datetime.now(UTC) - timedelta(days=1)
    db_session.add(
        AffiliateLedgerEntry(
            participant_id=participant.id,
            account="available",
            amount_cents=15000,
            entry_type="test",
            idempotency_key="journey-funds",
        )
    )
    await db_session.commit()
    body = {"amountCents": 15000, "requestId": "journey-request"}
    payout = await api_client.post(root + "/payouts", headers=headers, json=body)
    assert payout.status_code == 200
    repeated = await api_client.post(root + "/payouts", headers=headers, json=body)
    assert repeated.json()["id"] == payout.json()["id"]
    assert (
        await api_client.delete(root + f"/payouts/{uuid4()}", headers=headers)
    ).status_code == 404
    assert (
        await api_client.delete(
            root + "/payouts/" + payout.json()["id"], headers=headers
        )
    ).status_code == 200
    assert (await AffiliateService(db_session).balances(participant.id))[
        "available"
    ] == 15000
    assert (await api_client.post(root + "/logout", headers=headers)).status_code == 200
    assert (await api_client.get(root + "/me")).status_code == 401
