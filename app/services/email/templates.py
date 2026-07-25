"""Transactional email templates (subject + HTML + plain text)."""

from dataclasses import dataclass
from html import escape

PRODUCT_NAME = "Korus Fono"


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def _layout(title: str, inner_html: str) -> str:
    return f"""\
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; color: #1f2937; background-color: #f6f7fb; margin: 0; padding: 24px;">
    <div style="max-width: 560px; margin: 0 auto; background: #ffffff; border-radius: 16px; padding: 32px;">
      <h1 style="color: #0ea5a4; font-size: 20px; margin-top: 0;">{PRODUCT_NAME}</h1>
      <h2 style="font-size: 18px;">{title}</h2>
      {inner_html}
      <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
      <p style="font-size: 12px; color: #6b7280;">
        Esta é uma mensagem automática do {PRODUCT_NAME}. Por favor, não responda diretamente a este e-mail.
      </p>
    </div>
  </body>
</html>"""


def password_reset_email(
    user_name: str, reset_url: str, expires_minutes: int
) -> RenderedEmail:
    """Password recovery email."""
    subject = f"Redefinição de senha - {PRODUCT_NAME}"
    safe_name = escape(user_name, quote=True)
    safe_url = escape(reset_url, quote=True)
    inner = f"""
      <p>Olá {safe_name},</p>
      <p>Recebemos uma solicitação para redefinir a senha da sua conta no
      {PRODUCT_NAME}.</p>
      <p>Para criar uma nova senha, clique no botão abaixo:</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Redefinir senha
        </a>
      </p>
      <p>Este link é válido por {expires_minutes} minutos e pode ser usado apenas uma vez.</p>
      <p>Se você não solicitou esta redefinição, ignore este e-mail; sua senha
      permanecerá inalterada.</p>
    """
    text = (
        f"Olá {user_name},\n\n"
        f"Recebemos uma solicitação para redefinir a senha da sua conta no {PRODUCT_NAME}.\n"
        f"Redefina sua senha em: {reset_url}\n\n"
        f"Este link é válido por {expires_minutes} minutos e pode ser usado apenas uma vez.\n"
        "Se você não solicitou esta redefinição, ignore este e-mail.\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(subject=subject, html=_layout("Redefinição de senha", inner), text=text)


def email_verification_email(
    user_name: str, verify_url: str, expires_minutes: int
) -> RenderedEmail:
    """Email address confirmation after signup."""
    subject = f"Confirme seu e-mail - {PRODUCT_NAME}"
    safe_name = escape(user_name, quote=True)
    safe_url = escape(verify_url, quote=True)
    inner = f"""
      <p>Olá {safe_name},</p>
      <p>Bem-vindo(a) ao {PRODUCT_NAME}! Para ativar sua conta, confirme seu
      endereço de e-mail clicando no botão abaixo:</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Confirmar e-mail
        </a>
      </p>
      <p>Este link é válido por {expires_minutes} minutos e pode ser usado apenas uma vez.</p>
      <p>Se você não criou esta conta, ignore este e-mail.</p>
    """
    text = (
        f"Olá {user_name},\n\n"
        f"Bem-vindo(a) ao {PRODUCT_NAME}! Confirme seu e-mail em: {verify_url}\n\n"
        f"Este link é válido por {expires_minutes} minutos e pode ser usado apenas uma vez.\n"
        "Se você não criou esta conta, ignore este e-mail.\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(subject=subject, html=_layout("Confirme seu e-mail", inner), text=text)
