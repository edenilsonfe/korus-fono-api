"""Transactional email templates (subject + HTML + plain text)."""

from dataclasses import dataclass
from html import escape
from urllib.parse import urljoin

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


_MONTHS_PT_BR = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def _period_label(week_start, week_end) -> str:
    if week_start.month == week_end.month:
        return f"{week_start.day} a {week_end.day} de {_MONTHS_PT_BR[week_end.month - 1]}"
    return (
        f"{week_start.day} de {_MONTHS_PT_BR[week_start.month - 1]} a "
        f"{week_end.day} de {_MONTHS_PT_BR[week_end.month - 1]}"
    )


def _money(cents: int) -> str:
    value = f"{cents / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {value}"


def weekly_summary_email(
    *,
    week_start,
    week_end,
    appointments: dict,
    finance: dict,
    agenda_url: str,
    finance_url: str,
    support_url: str,
    unsubscribe_url: str,
) -> RenderedEmail:
    period = _period_label(week_start, week_end)
    subject = f"Seu resumo semanal | {period}"
    safe_agenda_url = escape(agenda_url, quote=True)
    safe_finance_url = escape(finance_url, quote=True)
    safe_support_url = escape(support_url, quote=True)
    safe_unsubscribe_url = escape(unsubscribe_url, quote=True)
    safe_logo_url = escape(
        urljoin(agenda_url, "/korusfono-mark-v2.png"), quote=True
    )
    rate = appointments["attendanceRate"]
    rate_label = "Sem base" if rate is None else f"{rate:g}%"
    overdue_count = finance["overdueCount"]
    overdue_label = (
        f"{overdue_count} conta" if overdue_count == 1 else f"{overdue_count} contas"
    )
    html = f"""\
<!doctype html>
<html lang="pt-BR" dir="ltr">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
    <title>{escape(subject)}</title>
    <style>
      @media only screen and (max-width: 620px) {{
        .email-shell {{ padding: 12px !important; }}
        .hero-cell {{ padding: 28px 22px !important; }}
        .content-cell {{ padding: 28px 20px !important; }}
        .metric-cell {{ padding: 14px !important; }}
        .footer-cell {{ padding: 24px 20px !important; }}
      }}
    </style>
  </head>
  <body style="font-family: 'Plus Jakarta Sans', Arial, Helvetica, sans-serif; color: #102a43; background-color: #f1f5f5; margin: 0; padding: 0;">
    <table lang="pt-BR" dir="ltr" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width: 100%; background-color: #f1f5f5;">
      <tr>
        <td class="email-shell" align="center" style="padding: 28px 12px;">
          <div style="display: none; max-height: 0; overflow: hidden; opacity: 0; color: transparent;">
            Agenda, comparecimento e caixa real da semana de {period}.
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width: 100%; max-width: 600px; background-color: #ffffff; border: 1px solid #dfe9e8; border-radius: 20px; box-shadow: 0 16px 40px rgba(6, 24, 39, 0.08); overflow: hidden;">
            <tr>
              <td style="height: 6px; line-height: 6px; background-color: #14a39c; font-size: 0;">&nbsp;</td>
            </tr>
            <tr>
              <td class="hero-cell" style="background-color: #061827; padding: 32px 36px 34px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="padding-right: 12px; vertical-align: middle;">
                      <img src="{safe_logo_url}" width="44" height="44" alt="" role="presentation" style="display: block; width: 44px; height: 44px; border: 0; border-radius: 11px;">
                    </td>
                    <td style="vertical-align: middle; color: #ffffff; font-size: 20px; font-weight: 800; letter-spacing: -0.5px;">
                      korus<span style="color: #62d5ca;">Fono</span>
                    </td>
                  </tr>
                </table>
                <p style="color: #62d5ca; font-size: 11px; font-weight: 800; letter-spacing: 1.6px; margin: 28px 0 10px; text-transform: uppercase;">Resumo semanal</p>
                <h1 style="color: #ffffff; font-size: 30px; line-height: 1.2; letter-spacing: -0.8px; margin: 0 0 10px;">Sua semana, em perspectiva.</h1>
                <p style="color: #bdd0d5; font-size: 15px; line-height: 1.6; margin: 0 0 20px;">Uma leitura rápida da sua operação, sem expor dados de pacientes.</p>
                <span style="display: inline-block; color: #e7fffc; background-color: #123b46; border: 1px solid #25616a; border-radius: 999px; font-size: 13px; font-weight: 700; padding: 8px 13px;">{period}</span>
              </td>
            </tr>
            <tr>
              <td class="content-cell" style="padding: 34px 36px 8px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="padding-bottom: 14px;">
                      <p style="color: #0f766e; font-size: 11px; font-weight: 800; letter-spacing: 1.4px; margin: 0 0 5px; text-transform: uppercase;">Agenda</p>
                      <h2 style="color: #102a43; font-size: 21px; line-height: 1.3; letter-spacing: -0.4px; margin: 0;">Como foi sua rotina clínica</h2>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border: 1px solid #c9e8e4; border-radius: 14px; background-color: #effaf8;">
                  <tr>
                    <td style="padding: 20px 22px;">
                      <p style="color: #476a6b; font-size: 12px; font-weight: 700; margin: 0 0 5px; text-transform: uppercase; letter-spacing: 0.8px;">Taxa de comparecimento</p>
                      <p style="color: #0f766e; font-size: 32px; font-weight: 800; letter-spacing: -1px; margin: 0;">{rate_label}</p>
                      <p style="color: #527477; font-size: 12px; line-height: 1.5; margin: 5px 0 0;">Calculada sobre atendimentos concluídos e faltas.</p>
                    </td>
                    <td align="right" style="padding: 20px 22px; vertical-align: middle;">
                      <p style="color: #476a6b; font-size: 12px; font-weight: 700; margin: 0 0 5px;">TOTAL DE HORÁRIOS</p>
                      <p style="color: #102a43; font-size: 28px; font-weight: 800; margin: 0;">{appointments['total']}</p>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 12px; table-layout: fixed;">
                  <tr>
                    <td class="metric-cell" width="50%" style="padding: 16px; border: 1px solid #e3eceb; border-radius: 12px 0 0 0;">
                      <p style="color: #0f766e; font-size: 23px; font-weight: 800; margin: 0 0 4px;">{appointments['completed']}</p>
                      <p style="color: #5d6f79; font-size: 13px; margin: 0;">Concluídos</p>
                    </td>
                    <td class="metric-cell" width="50%" style="padding: 16px; border: 1px solid #e3eceb; border-left: 0; border-radius: 0 12px 0 0;">
                      <p style="color: #bd5b4f; font-size: 23px; font-weight: 800; margin: 0 0 4px;">{appointments['noShow']}</p>
                      <p style="color: #5d6f79; font-size: 13px; margin: 0;">Faltas</p>
                    </td>
                  </tr>
                  <tr>
                    <td class="metric-cell" width="50%" style="padding: 16px; border: 1px solid #e3eceb; border-top: 0; border-radius: 0 0 0 12px;">
                      <p style="color: #697a84; font-size: 23px; font-weight: 800; margin: 0 0 4px;">{appointments['cancelled']}</p>
                      <p style="color: #5d6f79; font-size: 13px; margin: 0;">Cancelados</p>
                    </td>
                    <td class="metric-cell" width="50%" style="padding: 16px; border: 1px solid #e3eceb; border-top: 0; border-left: 0; border-radius: 0 0 12px 0;">
                      <p style="color: #735a94; font-size: 23px; font-weight: 800; margin: 0 0 4px;">{appointments['unfinished']}</p>
                      <p style="color: #5d6f79; font-size: 13px; margin: 0;">Não finalizados</p>
                    </td>
                  </tr>
                </table>
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0 34px;">
                  <tr>
                    <td style="background-color: #0f766e; border-radius: 10px;">
                      <a href="{safe_agenda_url}" style="display: inline-block; color: #ffffff; font-size: 14px; font-weight: 800; padding: 12px 18px; text-decoration: none;">Abrir minha agenda &nbsp;→</a>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top: 1px solid #e3eceb;">
                  <tr>
                    <td style="padding: 30px 0 14px;">
                      <p style="color: #0f766e; font-size: 11px; font-weight: 800; letter-spacing: 1.4px; margin: 0 0 5px; text-transform: uppercase;">Financeiro</p>
                      <h2 style="color: #102a43; font-size: 21px; line-height: 1.3; letter-spacing: -0.4px; margin: 0;">Seu caixa real da semana</h2>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #061827; border-radius: 14px;">
                  <tr>
                    <td style="padding: 21px 22px;">
                      <p style="color: #9bb7bd; font-size: 12px; font-weight: 700; letter-spacing: 0.7px; margin: 0 0 5px; text-transform: uppercase;">Saldo da semana</p>
                      <p style="color: #ffffff; font-size: 29px; font-weight: 800; letter-spacing: -0.7px; margin: 0;">{_money(finance['balanceCents'])}</p>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 12px; table-layout: fixed;">
                  <tr>
                    <td class="metric-cell" width="50%" style="padding: 16px; background-color: #f6faf9; border: 1px solid #e3eceb; border-radius: 12px 0 0 12px;">
                      <p style="color: #5d6f79; font-size: 12px; margin: 0 0 5px;">Recebimentos confirmados</p>
                      <p style="color: #0f766e; font-size: 18px; font-weight: 800; margin: 0;">{_money(finance['receivedCents'])}</p>
                    </td>
                    <td class="metric-cell" width="50%" style="padding: 16px; background-color: #f6faf9; border: 1px solid #e3eceb; border-left: 0; border-radius: 0 12px 12px 0;">
                      <p style="color: #5d6f79; font-size: 12px; margin: 0 0 5px;">Despesas pagas</p>
                      <p style="color: #102a43; font-size: 18px; font-weight: 800; margin: 0;">{_money(finance['paidExpensesCents'])}</p>
                    </td>
                  </tr>
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 12px; background-color: #fff5f1; border: 1px solid #f4d7cc; border-radius: 12px;">
                  <tr>
                    <td style="padding: 16px 18px;">
                      <p style="color: #8e483d; font-size: 12px; font-weight: 700; margin: 0 0 5px;">CONTAS VENCIDAS EM ABERTO</p>
                      <p style="color: #572f2a; font-size: 16px; line-height: 1.4; margin: 0;"><strong>{overdue_label}</strong> · {_money(finance['overdueBalanceCents'])}</p>
                    </td>
                  </tr>
                </table>
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin: 20px 0 30px;">
                  <tr>
                    <td style="border: 1px solid #0f766e; border-radius: 10px;">
                      <a href="{safe_finance_url}" style="display: inline-block; color: #0f766e; font-size: 14px; font-weight: 800; padding: 11px 17px; text-decoration: none;">Ver detalhes financeiros &nbsp;→</a>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td class="footer-cell" style="background-color: #f6faf9; border-top: 1px solid #e3eceb; padding: 25px 36px 28px;">
                <p style="color: #536a73; font-size: 12px; line-height: 1.6; margin: 0 0 10px;">Mensagem informativa do {PRODUCT_NAME}, sem dados identificáveis de pacientes.</p>
                <p style="font-size: 12px; line-height: 1.6; margin: 0;">
                  <a href="{safe_support_url}" style="color: #0f766e; font-weight: 700; text-decoration: underline;">Falar com o suporte</a>
                  <span style="color: #9aabae;">&nbsp;·&nbsp;</span>
                  <a href="{safe_unsubscribe_url}" style="color: #536a73; text-decoration: underline;">Desativar resumo semanal</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
    text = (
        f"{PRODUCT_NAME}\nSeu resumo semanal | {period}\n\n"
        "AGENDA\n"
        f"Total de horários: {appointments['total']}\n"
        f"Concluídos: {appointments['completed']}\n"
        f"Faltas: {appointments['noShow']}\n"
        f"Cancelados: {appointments['cancelled']}\n"
        f"Não finalizados: {appointments['unfinished']}\n"
        f"Taxa de comparecimento: {rate_label}\n"
        f"Ver agenda: {agenda_url}\n\n"
        "FINANCEIRO\n"
        f"Recebimentos confirmados: {_money(finance['receivedCents'])}\n"
        f"Despesas pagas: {_money(finance['paidExpensesCents'])}\n"
        f"Saldo da semana: {_money(finance['balanceCents'])}\n"
        f"Contas vencidas em aberto: {finance['overdueCount']}\n"
        f"Saldo vencido: {_money(finance['overdueBalanceCents'])}\n"
        f"Ver financeiro: {finance_url}\n\n"
        f"Suporte: {support_url}\n"
        f"Desativar resumo semanal: {unsubscribe_url}\n"
    )
    return RenderedEmail(subject=subject, html=html, text=text)


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


def care_team_invitation_email(
    recipient_name: str,
    inviter_name: str,
    invitation_url: str,
    expires_days: int,
) -> RenderedEmail:
    """Invitation without patient identifiers or clinical data."""
    subject = f"Convite para uma equipe assistencial - {PRODUCT_NAME}"
    safe_recipient = escape(recipient_name, quote=True)
    safe_inviter = escape(inviter_name, quote=True)
    safe_url = escape(invitation_url, quote=True)
    inner = f"""
      <p>Olá {safe_recipient},</p>
      <p>{safe_inviter} convidou você para participar de uma equipe assistencial no
      {PRODUCT_NAME}.</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Revisar convite
        </a>
      </p>
      <p>Entre com o e-mail que recebeu esta mensagem. O convite expira em
      {expires_days} dias e só pode ser usado uma vez.</p>
      <p>Se você não esperava este convite, ignore esta mensagem.</p>
    """
    text = (
        f"Olá {recipient_name},\n\n"
        f"{inviter_name} convidou você para uma equipe assistencial no {PRODUCT_NAME}.\n"
        f"Revise o convite em: {invitation_url}\n\n"
        f"O convite expira em {expires_days} dias e só pode ser usado uma vez.\n"
        "Se você não esperava este convite, ignore esta mensagem."
    )
    return RenderedEmail(
        subject=subject,
        html=_layout("Convite para equipe assistencial", inner),
        text=text,
    )


def trial_expiration_email(
    user_name: str,
    audience: str,
    trial_ends_at: str,
    plans_url: str,
) -> RenderedEmail:
    """Engagement email for a trial that expired or is close to expiring."""
    safe_name = escape(user_name, quote=True)
    safe_url = escape(plans_url, quote=True)
    safe_date = escape(trial_ends_at, quote=True)

    if audience == "expired":
        subject = f"Seu período de teste terminou - {PRODUCT_NAME}"
        title = "Seu trial terminou"
        lead = (
            f"Seu período de teste terminou em <strong>{safe_date}</strong>. "
            "Se quiser continuar usando os protocolos, scoring e recursos clínicos, "
            "escolha o plano que combina com sua rotina."
        )
        plain_lead = (
            f"Seu período de teste terminou em {trial_ends_at}. "
            "Para continuar usando os recursos do Korus Fono, escolha um plano."
        )
    else:
        subject = f"Seu período de teste está terminando - {PRODUCT_NAME}"
        title = "Seu trial está terminando"
        lead = (
            f"Seu período de teste vai até <strong>{safe_date}</strong>. "
            "Escolha um plano para manter o acesso de escrita aos seus fluxos clínicos "
            "sem interrupção."
        )
        plain_lead = (
            f"Seu período de teste vai até {trial_ends_at}. "
            "Escolha um plano para manter seus fluxos clínicos sem interrupção."
        )

    inner = f"""
      <p>Olá {safe_name},</p>
      <p>{lead}</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Ver planos
        </a>
      </p>
      <p>Se precisar de ajuda para escolher, fale com o suporte pela plataforma.</p>
    """
    text = (
        f"Olá {user_name},\n\n"
        f"{plain_lead}\n\n"
        f"Veja os planos: {plans_url}\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(subject=subject, html=_layout(title, inner), text=text)


def new_account_notification_email(
    user_name: str,
    user_email: str,
    specialty: str,
    council: str,
    phone: str,
    created_at: str,
    trial_ends_at: str,
) -> RenderedEmail:
    """Internal notification when a new professional account is created."""
    subject = f"Novo cadastro no {PRODUCT_NAME}"
    safe_name = escape(user_name, quote=True)
    safe_email = escape(user_email, quote=True)
    safe_specialty = escape(specialty, quote=True)
    safe_council = escape(council or "—", quote=True)
    safe_phone = escape(phone or "—", quote=True)
    inner = f"""
      <p>Uma nova conta foi criada no {PRODUCT_NAME}:</p>
      <table style="border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px;">
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280; width: 40%;">Nome</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;"><strong>{safe_name}</strong></td>
        </tr>
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">E-mail</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">{safe_email}</td>
        </tr>
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">Especialidade</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">{safe_specialty}</td>
        </tr>
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">Registro</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">{safe_council}</td>
        </tr>
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb; color: #6b7280;">Telefone</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e5e7eb;">{safe_phone}</td>
        </tr>
      </table>
      <p style="font-size: 13px; color: #6b7280;">
        Cadastro em {created_at} &middot; Trial até {trial_ends_at}
      </p>
    """
    text = (
        f"Uma nova conta foi criada no {PRODUCT_NAME}.\n\n"
        f"Nome: {user_name}\n"
        f"E-mail: {user_email}\n"
        f"Especialidade: {specialty}\n"
        f"Registro: {council or '—'}\n"
        f"Telefone: {phone or '—'}\n\n"
        f"Cadastro em: {created_at}\n"
        f"Trial até: {trial_ends_at}\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(subject=subject, html=_layout("Novo cadastro", inner), text=text)


def report_delivery_email(
    professional_name: str,
    patient_name: str,
    report_label: str,
    delivery_url: str,
    expires_days: int,
) -> RenderedEmail:
    """Delivery of a finalized clinical document to a caregiver/school e-mail."""
    subject = f"{report_label} — {patient_name}"
    safe_professional = escape(professional_name, quote=True)
    safe_patient = escape(patient_name, quote=True)
    safe_label = escape(report_label, quote=True)
    safe_url = escape(delivery_url, quote=True)
    inner = f"""
      <p>Olá,</p>
      <p><strong>{safe_professional}</strong> enviou o documento
      <strong>{safe_label}</strong> de {safe_patient}.</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Abrir documento
        </a>
      </p>
      <p>Este link é pessoal e válido por {expires_days} dias. Não o compartilhe
      sem necessidade.</p>
    """
    text = (
        f"{professional_name} enviou o documento \"{report_label}\" de {patient_name}.\n\n"
        f"Acesse: {delivery_url}\n\n"
        f"Este link é pessoal e válido por {expires_days} dias.\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(subject=subject, html=_layout(report_label, inner), text=text)


def school_report_delivery_email(
    professional_name: str,
    school_name: str,
    school_recipient_name: str,
    delivery_url: str,
    expires_days: int,
) -> RenderedEmail:
    """School delivery notice (F20), minimized on purpose: generic subject, no
    patient name, diagnosis or attachment — just the school, the link and its
    validity. Reuses the shared layout and escapes every user-provided value."""
    subject = "Documento escolar disponível"
    safe_professional = escape(professional_name, quote=True)
    safe_school = escape(school_name, quote=True)
    safe_recipient = escape(school_recipient_name, quote=True)
    safe_url = escape(delivery_url, quote=True)
    inner = f"""
      <p>Olá, {safe_recipient}.</p>
      <p><strong>{safe_professional}</strong> disponibilizou um documento escolar
      de <strong>{safe_school}</strong> no {PRODUCT_NAME}.</p>
      <p style="margin: 28px 0;">
        <a href="{safe_url}"
           style="background: #0ea5a4; color: #ffffff; text-decoration: none; padding: 12px 20px; border-radius: 9999px;">
          Abrir documento
        </a>
      </p>
      <p>Este link é pessoal e válido por {expires_days} dias. Não o compartilhe
      sem necessidade.</p>
    """
    text = (
        f"Olá, {school_recipient_name}.\n\n"
        f"{professional_name} disponibilizou um documento escolar de "
        f"{school_name} no {PRODUCT_NAME}.\n\n"
        f"Acesse: {delivery_url}\n\n"
        f"Este link é pessoal e válido por {expires_days} dias. "
        "Não o compartilhe sem necessidade.\n\n"
        f"Atenciosamente,\n{PRODUCT_NAME}"
    )
    return RenderedEmail(
        subject=subject, html=_layout("Documento escolar disponível", inner), text=text
    )
