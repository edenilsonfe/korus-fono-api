"""Tests for the new-account notification email (service + HTTP flow)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.services.email.templates import new_account_notification_email
from app.services.new_account_notification import send_new_account_notification_sync


def test_new_account_notification_email_template_copy():
    rendered = new_account_notification_email(
        user_name="Ana <Fono>",
        user_email="ana@example.com",
        specialty="Fonoaudiologia",
        council="CRFa 12345",
        phone="(11) 99999-0000",
        created_at="01/08/2026 11:30",
        trial_ends_at="08/08/2026",
    )

    assert "Novo cadastro" in rendered.subject
    assert "Ana &lt;Fono&gt;" in rendered.html
    assert "ana@example.com" in rendered.html
    assert "Fonoaudiologia" in rendered.html
    assert "CRFa 12345" in rendered.text
    assert "(11) 99999-0000" in rendered.text
    assert "01/08/2026 11:30" in rendered.text
    assert "08/08/2026" in rendered.text


def test_send_new_account_notification_sync_sends_when_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_sending_enabled", True)
    monkeypatch.setattr(settings, "new_account_notification_email", "owner@example.com")

    send_mock = MagicMock(return_value="msg-id")
    monkeypatch.setattr("app.services.new_account_notification.send_email", send_mock)

    send_new_account_notification_sync(
        user_name="Ana",
        user_email="ana@example.com",
        specialty="Fonoaudiologia",
        council="",
        phone="",
        created_at=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
        trial_ends_at=datetime(2026, 8, 8, tzinfo=UTC),
    )

    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "owner@example.com"
    assert "ana@example.com" in kwargs["html"]
    assert "Novo cadastro" in kwargs["subject"]


def test_send_new_account_notification_sync_skips_without_recipient(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_sending_enabled", True)
    monkeypatch.setattr(settings, "new_account_notification_email", "")

    send_mock = MagicMock()
    monkeypatch.setattr("app.services.new_account_notification.send_email", send_mock)

    send_new_account_notification_sync(
        user_name="Ana",
        user_email="ana@example.com",
        specialty="Fonoaudiologia",
        council="",
        phone="",
        created_at=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
        trial_ends_at=None,
    )

    send_mock.assert_not_called()


def test_send_new_account_notification_sync_skips_when_email_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_sending_enabled", False)
    monkeypatch.setattr(settings, "new_account_notification_email", "owner@example.com")

    send_mock = MagicMock()
    monkeypatch.setattr("app.services.new_account_notification.send_email", send_mock)

    send_new_account_notification_sync(
        user_name="Ana",
        user_email="ana@example.com",
        specialty="Fonoaudiologia",
        council="",
        phone="",
        created_at=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
        trial_ends_at=None,
    )

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_http_register_queues_notification_task(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.auth.enforce_register_rate_limit", lambda *_a, **_k: None
    )

    task_mock = MagicMock()
    monkeypatch.setattr("app.api.v1.auth.send_new_account_notification_task", task_mock)

    email = f"notify-{uuid4().hex[:8]}@test.com"
    reg = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "securepass123",
            "name": "Notify User",
            "specialtyKey": "fono",
            "council": "CRFa 999",
            "phone": "(11) 98888-7777",
        },
    )
    assert reg.status_code == 201
    assert task_mock.called
    args = task_mock.call_args.args
    assert args[0] == "Notify User"
    assert args[1] == email
    assert args[2] == "Fonoaudiologia"
    assert args[3] == "CRFa 999"
    assert args[4] == "(11) 98888-7777"
