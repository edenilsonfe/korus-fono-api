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
