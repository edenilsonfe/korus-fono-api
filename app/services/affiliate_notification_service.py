"""Privacy-safe affiliate notifications for the app inbox and partner email."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.affiliate import AffiliateParticipant
from app.models.app_notification import AppNotification
from app.services.email.resend_client import send_email

logger = logging.getLogger(__name__)


class AffiliateNotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def notify(
        self,
        *,
        participant: AffiliateParticipant,
        event_type: str,
        title: str,
        body: str,
        severity: str = "info",
    ) -> None:
        if participant.professional_id is not None:
            self.db.add(
                AppNotification(
                    kind="personal",
                    type=event_type[:20],
                    title=title[:200],
                    body=body,
                    deep_link="/indicacoes",
                    severity=severity,
                    recipient_professional_id=participant.professional_id,
                    status="published",
                    publish_at=datetime.now(UTC),
                )
            )
            await self.db.flush()
        if participant.partner_enabled and get_settings().email_sending_enabled:
            try:
                await asyncio.to_thread(
                    send_email,
                    participant.email,
                    title,
                    f"<p>{body}</p><p>Acesse o portal de afiliados para ver os detalhes.</p>",
                    f"{body}\n\nAcesse o portal de afiliados para ver os detalhes.",
                )
            except Exception as exc:
                logger.warning(
                    "Affiliate event email failed for participant %s: %s",
                    participant.id,
                    exc,
                )
