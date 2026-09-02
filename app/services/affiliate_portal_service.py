"""One-use magic links and scoped eight-hour affiliate portal sessions."""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import quote_plus
from uuid import UUID

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.affiliate import AffiliateMagicLink, AffiliateParticipant
from app.services.email.resend_client import send_email

logger = logging.getLogger(__name__)


class AffiliatePortalForbiddenError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AffiliatePortalService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_magic_link(self, participant: AffiliateParticipant) -> str:
        if participant.status not in {"invited", "active"} or not participant.partner_enabled:
            raise AffiliatePortalForbiddenError("Participante não pode acessar o portal")
        now = datetime.now(UTC)
        await self.db.execute(
            update(AffiliateMagicLink)
            .where(
                AffiliateMagicLink.participant_id == participant.id,
                AffiliateMagicLink.used_at.is_(None),
            )
            .values(used_at=now)
        )
        raw = secrets.token_urlsafe(32)
        self.db.add(
            AffiliateMagicLink(
                participant_id=participant.id,
                token_hash=_hash(raw),
                expires_at=now + timedelta(minutes=15),
            )
        )
        await self.db.flush()
        return raw

    async def request_magic_link(self, email: str) -> tuple[AffiliateParticipant, str] | None:
        participant = (
            await self.db.execute(
                select(AffiliateParticipant).where(
                    AffiliateParticipant.email == email.strip().lower(),
                    AffiliateParticipant.partner_enabled.is_(True),
                    AffiliateParticipant.status.in_(["invited", "active"]),
                )
            )
        ).scalar_one_or_none()
        if participant is None:
            return None
        return participant, await self.create_magic_link(participant)

    async def exchange_magic_link(self, raw_token: str) -> AffiliateParticipant:
        row = (
            await self.db.execute(
                select(AffiliateMagicLink).where(AffiliateMagicLink.token_hash == _hash(raw_token))
            )
        ).scalar_one_or_none()
        if row is None:
            raise AffiliatePortalForbiddenError("Link inválido ou expirado")
        if row.used_at is not None:
            raise AffiliatePortalForbiddenError("Este link já foi usado")
        if _as_utc(row.expires_at) <= datetime.now(UTC):
            raise AffiliatePortalForbiddenError("Link inválido ou expirado")
        participant = await self.db.get(AffiliateParticipant, row.participant_id)
        if participant is None or participant.status not in {"invited", "active"}:
            raise AffiliatePortalForbiddenError("Participante não pode acessar o portal")
        row.used_at = datetime.now(UTC)
        await self.db.flush()
        return participant

    @staticmethod
    def create_session_token(participant: AffiliateParticipant) -> str:
        settings = get_settings()
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": str(participant.id),
                "type": "affiliate_portal",
                "iat": now,
                "exp": now + timedelta(hours=8),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )

    @staticmethod
    def decode_session_token(token: str) -> UUID:
        settings = get_settings()
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
            if payload.get("type") != "affiliate_portal":
                raise ValueError("scope")
            return UUID(str(payload["sub"]))
        except Exception as exc:
            raise AffiliatePortalForbiddenError("Sessão do portal inválida") from exc


def send_affiliate_magic_link_email(to_email: str, public_name: str | None, raw_token: str) -> None:
    settings = get_settings()
    url = f"{settings.frontend_url.rstrip('/')}/afiliados?token={quote_plus(raw_token)}"
    if not settings.email_sending_enabled:
        logger.info("Email sending disabled; affiliate portal token created (recipient omitted)")
        return
    name = public_name or "parceiro"
    html_name = escape(name)
    send_email(
        to_email=to_email,
        subject="Seu acesso ao portal de afiliados KorusFono",
        html=(
            f"<p>Olá, {html_name}.</p><p>Este link é válido por 15 minutos e pode ser usado uma vez.</p>"
            f'<p><a href="{url}">Acessar portal de afiliados</a></p>'
        ),
        text=(
            f"Olá, {name}. Seu link de acesso, válido por 15 minutos e de uso único: {url}"
        ),
    )
