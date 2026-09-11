"""Opt-in reassessment reminders: eligibility, dispatch, settings and dashboard."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.assessment import Assessment
from app.models.caregiver import Caregiver
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.services.reassessment_service import (
    list_due_reassessments,
    months_ago,
    run_reassessment_messages,
)
from app.services.whatsapp_types import WhatsAppSendResult

TODAY = date(2026, 9, 3)
NOW = datetime(2026, 9, 3, 15, tzinfo=UTC)  # noon in Sao Paulo


@pytest.fixture
def clinic_clock(monkeypatch):
    monkeypatch.setattr(get_settings(), "clinic_timezone", "America/Sao_Paulo")


@pytest.fixture
async def reminder_settings(db_session, professional, patient):
    settings = NotificationSettings(
        professional_id=professional.id,
        whatsapp_enabled=True,
        whatsapp_events={"reassessment_reminder": True},
        whatsapp_message_templates={},
    )
    db_session.add(settings)
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    caregiver.whatsapp_opt_in = True
    await db_session.commit()
    return settings


@pytest.fixture
async def overdue_assessment(db_session, patient):
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=patient.professional_id,
        protocol_id="portage",
        date=date(2025, 12, 1),
        result="Atraso leve",
        percentage=40,
        interpretation="",
        fields=[],
        answers={},
        scores={"total": 10},
        status="completed",
    )
    db_session.add(assessment)
    await db_session.commit()
    return assessment


@pytest.fixture
def whatsapp_provider(monkeypatch):
    provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution",
                provider_message_id="reassessment-1",
                status="sent",
                payload={},
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    return provider


def test_months_ago_is_calendar_accurate():
    assert months_ago(date(2026, 9, 3), 6) == date(2026, 3, 3)
    assert months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_ago(date(2026, 1, 15), 1) == date(2025, 12, 15)


async def test_list_due_reassessments_flags_only_overdue(
    db_session, professional, patient, reminder_settings, overdue_assessment, clinic_clock
):
    due = await list_due_reassessments(
        db_session, professional_id=professional.id, today=TODAY
    )
    assert [row[0] for row in due] == [patient.id]
    assert due[0][2] == date(2025, 12, 1)
    assert due[0][3] == 6

    # Only three months later the patient is not due yet.
    not_due = await list_due_reassessments(
        db_session, professional_id=professional.id, today=date(2026, 2, 1)
    )
    assert not_due == []


async def test_run_reminders_queues_and_sends(
    db_session, professional, patient, reminder_settings, overdue_assessment,
    whatsapp_provider, clinic_clock,
):
    sent = await run_reassessment_messages(db_session, NOW)
    assert sent == 1
    log = await db_session.scalar(
        select(NotificationMessageLog).where(
            NotificationMessageLog.notification_type == "reassessment_reminder"
        )
    )
    assert log is not None
    assert log.status == "sent"
    assert log.patient_id == patient.id
    assert "reavaliação" in (
        whatsapp_provider.send_text_message.await_args.args[2]
    )
    assert "01/12/2025" in whatsapp_provider.send_text_message.await_args.args[2]
    # A second run does not duplicate the reminder for the same assessment gap.
    again = await run_reassessment_messages(db_session, NOW)
    assert again == 0
    count = len(
        (
            await db_session.scalars(
                select(NotificationMessageLog).where(
                    NotificationMessageLog.notification_type == "reassessment_reminder"
                )
            )
        ).all()
    )
    assert count == 1


async def test_event_toggle_off_does_not_queue(
    db_session, professional, patient, overdue_assessment, whatsapp_provider, clinic_clock
):
    db_session.add(
        NotificationSettings(
            professional_id=professional.id,
            whatsapp_enabled=True,
            whatsapp_events={"reassessment_reminder": False},
            whatsapp_message_templates={},
        )
    )
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    caregiver.whatsapp_opt_in = True
    await db_session.commit()
    sent = await run_reassessment_messages(db_session, NOW)
    assert sent == 0
    logs = (
        await db_session.scalars(
            select(NotificationMessageLog).where(
                NotificationMessageLog.notification_type == "reassessment_reminder"
            )
        )
    ).all()
    assert logs == []


async def test_new_assessment_supersedes_queued_reminder(
    db_session, professional, patient, reminder_settings, overdue_assessment,
    monkeypatch, clinic_clock,
):
    blocked = SimpleNamespace(can_send=AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda db: blocked,
    )
    sent = await run_reassessment_messages(db_session, NOW)
    assert sent == 0
    log = await db_session.scalar(
        select(NotificationMessageLog).where(
            NotificationMessageLog.notification_type == "reassessment_reminder"
        )
    )
    assert log is not None and log.status == "queued"

    # A newer completed assessment ends the reminder cycle.
    db_session.add(
        Assessment(
            patient_id=patient.id,
            professional_id=professional.id,
            protocol_id="portage",
            date=date(2026, 8, 20),
            result="Reavaliação recente",
            percentage=70,
            interpretation="",
            fields=[],
            answers={},
            scores={"total": 20},
            status="completed",
        )
    )
    await db_session.commit()

    provider = SimpleNamespace(
        can_send=AsyncMock(return_value=True),
        send_text_message=AsyncMock(
            return_value=WhatsAppSendResult(
                provider="evolution", provider_message_id="x", status="sent", payload={}
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.whatsapp_notification_service.get_active_whatsapp_provider",
        lambda db: provider,
    )
    sent = await run_reassessment_messages(db_session, NOW)
    assert sent == 0
    provider.send_text_message.assert_not_awaited()
    await db_session.refresh(log)
    assert log.status == "superseded"
    assert (log.payload or {}).get("skip_reason") == "reassessment_completed"


async def test_dashboard_reports_due_reassessments(
    db_session, professional, patient, reminder_settings, overdue_assessment, clinic_clock
):
    from app.services.dashboard import build_dashboard

    data = await build_dashboard(db_session, professional.id)
    assert data["pending"]["reassessmentDue"] == 1
    assert any(
        suggestion["id"] == "pending-reassessment"
        for suggestion in data["suggestions"]
    )


async def test_whatsapp_settings_roundtrip_for_reassessment(
    api_client, auth_headers, db_session, professional
):
    response = await api_client.get("/api/v1/whatsapp/settings", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["reassessmentReminderMonths"] == 6
    assert body["noShowPolicy"] is None
    assert body["whatsappEvents"]["reassessmentReminder"] is False
    assert "reassessment_reminder" in body["templateDefaults"]

    updated = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "reassessmentReminderMonths": 9,
            "noShowPolicy": "Faltas sem aviso prévio podem ser cobradas.",
            "whatsappEvents": {"reassessmentReminder": True},
            "whatsappMessageTemplates": {
                "reassessment_reminder": "Olá! Bora reavaliar, {{nomeResponsavel}}?"
            },
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["reassessmentReminderMonths"] == 9
    assert body["noShowPolicy"] == "Faltas sem aviso prévio podem ser cobradas."
    assert body["whatsappEvents"]["reassessmentReminder"] is True
    assert body["whatsappMessageTemplates"]["reassessment_reminder"].startswith("Olá!")

    stored = await db_session.scalar(
        select(NotificationSettings).where(
            NotificationSettings.professional_id == professional.id
        )
    )
    await db_session.refresh(stored)
    assert stored.reassessment_reminder_months == 9
    assert stored.no_show_policy.startswith("Faltas")


async def test_settings_validate_months_range(api_client, auth_headers):
    too_small = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={"reassessmentReminderMonths": 0},
    )
    assert too_small.status_code == 422
    too_big = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={"reassessmentReminderMonths": 30},
    )
    assert too_big.status_code == 422
