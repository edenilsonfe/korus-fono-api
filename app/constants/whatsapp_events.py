"""WhatsApp notification event identifiers and message copy."""

from __future__ import annotations

import re
from typing import Any

WHATSAPP_EVENT_REMINDER_24H = "appointment_reminder_24h"
WHATSAPP_EVENT_CONFIRMATION = "appointment_confirmation"
WHATSAPP_EVENT_CANCELLED = "appointment_cancelled"
WHATSAPP_EVENT_RESCHEDULED = "appointment_rescheduled"
WHATSAPP_EVENT_BILLING_REMINDER = "billing_reminder"
WHATSAPP_EVENT_BILLING_OVERDUE = "billing_overdue"
WHATSAPP_EVENT_WELCOME = "welcome"

WHATSAPP_EVENT_IDS: tuple[str, ...] = (
    WHATSAPP_EVENT_REMINDER_24H,
    WHATSAPP_EVENT_CONFIRMATION,
    WHATSAPP_EVENT_CANCELLED,
    WHATSAPP_EVENT_RESCHEDULED,
    WHATSAPP_EVENT_BILLING_REMINDER,
    WHATSAPP_EVENT_BILLING_OVERDUE,
)

DEFAULT_WHATSAPP_EVENTS: dict[str, bool] = {event_id: False for event_id in WHATSAPP_EVENT_IDS}

APPOINTMENT_NOTIFICATION_EVENT_MAP: dict[str, str] = {
    "confirmation": WHATSAPP_EVENT_CONFIRMATION,
    "rescheduled": WHATSAPP_EVENT_RESCHEDULED,
    "cancelled": WHATSAPP_EVENT_CANCELLED,
}

REMINDER_TEMPLATE_BODY = (
    "Olá, {{1}}. Tudo bem?\n\n"
    "Lembrando que sua sessão com {{2}} está marcada para:\n\n"
    "🗓️ {{3}}\n"
    "⏰ {{4}}\n"
    "📍 {{5}}\n\n"
    "Qualquer imprevisto, me avise com antecedência."
)

# Welcome message sent by the platform's own number right after registration.
# Variable: {{firstName}}.
DEFAULT_WELCOME_MESSAGE = (
    "Olá, {{firstName}}! 👋\n\n"
    "Seja bem-vindo(a) ao Korus Fono! 🎉\n\n"
    "Sua conta foi criada com sucesso e você já pode começar a usar a plataforma: "
    "organize sua agenda, aplique protocolos e acompanhe a evolução dos seus pacientes.\n\n"
    "Se tiver qualquer dúvida ou precisar de ajuda, nossa equipe está à disposição — "
    "é só responder esta mensagem. 💬\n\n"
    "Um abraço,\n"
    "Equipe Korus Fono"
)

WELCOME_MESSAGE_MAX_LENGTH = 4000

DEFAULT_EVENT_MESSAGE_TEMPLATES: dict[str, str] = {
    WHATSAPP_EVENT_CONFIRMATION: (
        "Olá, {{patientName}}. Tudo bem?\n\n"
        "Sua sessão com {{clinicianName}} foi confirmada para:\n\n"
        "🗓️ {{appointmentDate}}\n"
        "⏰ {{appointmentTime}}\n"
        "📍 {{clinicName}}\n\n"
        "Qualquer dúvida, estou à disposição."
    ),
    WHATSAPP_EVENT_CANCELLED: (
        "Olá, {{patientName}}. Tudo bem?\n\n"
        "Por um imprevisto, precisaremos cancelar o atendimento que estava marcado para:\n\n"
        "🗓️ {{appointmentDate}}\n"
        "⏰ {{appointmentTime}}\n\n"
        "Peço desculpas por isso. Quero te atender no melhor horário possível, "
        "então me diga quando fica melhor para você.\n\n"
        "Combinado?"
    ),
    WHATSAPP_EVENT_RESCHEDULED: (
        "Olá, {{patientName}}. Tudo bem?\n\n"
        "Seu atendimento com {{clinicianName}} foi reagendado para:\n\n"
        "🗓️ {{appointmentDate}}\n"
        "⏰ {{appointmentTime}}\n"
        "📍 {{clinicName}}\n\n"
        "Se precisar de outro horário, é só me avisar."
    ),
    WHATSAPP_EVENT_BILLING_REMINDER: (
        "Olá, {{patientName}}. Tudo bem?\n\n"
        "Lembramos que há um pagamento pendente de R$ {{amount}} "
        "com vencimento em {{dueDate}}.\n\n"
        "Qualquer dúvida, estou à disposição."
    ),
    WHATSAPP_EVENT_BILLING_OVERDUE: (
        "Olá, {{patientName}}. Tudo bem?\n\n"
        "Identificamos um pagamento em atraso de R$ {{amount}} "
        "(vencimento {{dueDate}}).\n\n"
        "Entre em contato para regularizar."
    ),
}

EVENT_MESSAGE_TEMPLATES = DEFAULT_EVENT_MESSAGE_TEMPLATES
PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")

ACTIVE_APPOINTMENT_STATUSES = ("pendente", "confirmado")


def _first_name(full_name: str | None) -> str:
    if not full_name or not full_name.strip():
        return ""
    return full_name.strip().split()[0]


def build_template_context(raw: dict[str, str]) -> dict[str, str]:
    patient_name = raw.get("patient_name", "")
    professional_name = raw.get("professional_name", "")
    return {
        "patientName": raw.get("patient_first_name") or _first_name(patient_name) or patient_name,
        "clinicianName": raw.get("professional_first_name")
        or _first_name(professional_name)
        or professional_name,
        "appointmentDate": raw.get("appointment_date", ""),
        "appointmentTime": raw.get("appointment_time", ""),
        "appointmentType": raw.get("appointment_type", ""),
        "clinicName": raw.get("clinic_name", ""),
        "amount": raw.get("amount", ""),
        "dueDate": raw.get("due_date", ""),
    }


def normalize_whatsapp_events(raw: Any) -> dict[str, bool]:
    events = dict(DEFAULT_WHATSAPP_EVENTS)
    if isinstance(raw, dict):
        for event_id in WHATSAPP_EVENT_IDS:
            if event_id in raw:
                events[event_id] = bool(raw[event_id])
    return events


def normalize_whatsapp_message_templates(raw: Any) -> dict[str, str]:
    templates: dict[str, str] = {}
    if isinstance(raw, dict):
        for event_id in WHATSAPP_EVENT_IDS:
            value = raw.get(event_id)
            if isinstance(value, str) and value.strip():
                templates[event_id] = value.strip()
    return templates


def merge_whatsapp_events(
    current: dict[str, bool], updates: dict[str, bool | None]
) -> dict[str, bool]:
    merged = dict(current)
    for event_id, value in updates.items():
        if event_id in WHATSAPP_EVENT_IDS and value is not None:
            merged[event_id] = bool(value)
    return merged


def merge_whatsapp_message_templates(
    current: dict[str, str], updates: dict[str, str | None]
) -> dict[str, str]:
    merged = dict(current)
    for event_id, value in updates.items():
        if event_id not in WHATSAPP_EVENT_IDS:
            continue
        if value is None or not str(value).strip():
            merged.pop(event_id, None)
        else:
            merged[event_id] = str(value).strip()
    return merged


def resolve_message_template(
    event_id: str, stored_templates: dict[str, str] | None = None
) -> str:
    custom = (stored_templates or {}).get(event_id)
    if custom:
        return custom
    template = DEFAULT_EVENT_MESSAGE_TEMPLATES.get(event_id)
    if not template:
        raise ValueError(f"No message template for event {event_id}")
    return template


def format_event_message(
    event_id: str,
    context: dict[str, str],
    *,
    custom_template: str | None = None,
    stored_templates: dict[str, str] | None = None,
) -> str:
    template = custom_template or resolve_message_template(event_id, stored_templates)
    resolved = build_template_context(context)

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        return resolved.get(key, match.group(0))

    return PLACEHOLDER_PATTERN.sub(replacer, template)
