"""Envio do form de contato do suporte para o e-mail da equipe."""

import logging

from app.core.config import get_settings
from app.services.email.resend_client import send_email

logger = logging.getLogger(__name__)


def send_support_contact_sync(
    *,
    professional_name: str,
    professional_email: str,
    subject: str,
    message: str,
) -> str | None:
    """Monta e envia o e-mail de contato; None se SUPPORT_CONTACT_EMAIL vazio."""
    settings = get_settings()
    recipient = (settings.support_contact_email or "").strip()
    if not recipient:
        logger.warning("SUPPORT_CONTACT_EMAIL not configured; skipping support contact")
        return None

    text = (
        f"Contato via página de suporte.\n\n"
        f"Nome: {professional_name}\n"
        f"E-mail: {professional_email}\n\n"
        f"Assunto: {subject}\n\n"
        f"{message}\n"
    )
    html = (
        "<p><strong>Contato via página de suporte</strong></p>"
        f"<p>Nome: {professional_name}<br>E-mail: {professional_email}</p>"
        f"<p><strong>Assunto:</strong> {subject}</p>"
        f"<p>{message}</p>"
    )
    return send_email(
        to_email=recipient,
        subject=f"[Suporte KorusFono] {subject}",
        html=html,
        text=text,
    )
