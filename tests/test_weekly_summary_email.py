import base64
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.finance import FinancialPayment, Payable, PayableSettlement, Receivable
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.weekly_summary_email import WeeklySummaryEmailDelivery
from app.services.weekly_summary_email_service import (
    create_weekly_summary_unsubscribe_token,
    run_weekly_summary_emails,
)


async def test_public_unsubscribe_is_idempotent_and_only_disables_weekly_summary(
    api_client, db_session, professional
):
    settings = NotificationSettings(
        professional_id=professional.id,
        birthday_in_app_enabled=True,
        weekly_summary_email_enabled=True,
        weekly_summary_email_preference_source="settings",
    )
    db_session.add(settings)
    await db_session.commit()
    invalid = await api_client.post(
        "/api/v1/notifications/weekly-summary/unsubscribe",
        params={"token": "!" * 20},
    )
    assert invalid.status_code == 400
    token = create_weekly_summary_unsubscribe_token(professional.id)

    landing = await api_client.get(
        "/api/v1/notifications/weekly-summary/unsubscribe",
        params={"token": token},
    )
    assert landing.status_code == 200
    assert "Desativar resumo semanal" in landing.text
    await db_session.refresh(settings)
    assert settings.weekly_summary_email_enabled is True

    for _ in range(2):
        response = await api_client.post(
            "/api/v1/notifications/weekly-summary/unsubscribe",
            params={"token": token},
        )
        assert response.status_code == 200
        assert response.json() == {"message": "Resumo semanal desativado."}

    stored = await db_session.scalar(
        select(NotificationSettings).where(
            NotificationSettings.professional_id == professional.id
        )
    )
    assert stored.weekly_summary_email_enabled is False
    assert stored.weekly_summary_email_opted_out_at is not None
    assert stored.weekly_summary_email_preference_source == "unsubscribe_link"
    assert stored.birthday_in_app_enabled is True


async def test_weekly_job_sends_one_aggregate_snapshot_without_patient_names(
    db_session, professional, patient, monkeypatch
):
    professional.subscription_status = "trialing"
    professional.trial_ends_at = datetime(2026, 9, 30, tzinfo=UTC)
    settings = NotificationSettings(
        professional_id=professional.id,
        weekly_summary_email_enabled=True,
        weekly_summary_email_opted_in_at=datetime(2026, 9, 18, tzinfo=UTC),
        weekly_summary_email_preference_source="settings",
    )
    db_session.add(settings)

    week_start = date(2026, 9, 14)
    statuses = ["concluido", "falta", "cancelado", "confirmado", "pendente"]
    for index, status in enumerate(statuses):
        db_session.add(
            Appointment(
                professional_id=professional.id,
                patient_id=patient.id,
                date=week_start + timedelta(days=index),
                time=time(9),
                type="Fonoaudiologia",
                duration=50,
                status=status,
            )
        )
    demo = Patient(
        professional_id=professional.id,
        name="Paciente Demonstração",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=week_start,
        avatar_color="oklch(0.5 0.1 200)",
        is_demo=True,
    )
    db_session.add(demo)
    await db_session.flush()
    db_session.add(
        Appointment(
            professional_id=professional.id,
            patient_id=demo.id,
            date=week_start,
            time=time(10),
            type="Demonstração",
            duration=50,
            status="concluido",
        )
    )
    db_session.add_all(
        [
            Appointment(
                professional_id=professional.id,
                patient_id=patient.id,
                date=date(2026, 9, 19),
                time=time(18),
                type="Fonoaudiologia",
                duration=50,
                status="confirmado",
            ),
            Appointment(
                professional_id=professional.id,
                patient_id=patient.id,
                date=date(2026, 9, 19),
                time=time(19),
                type="Fonoaudiologia",
                duration=50,
                status="pendente",
            ),
        ]
    )
    db_session.add(
        FinancialPayment(
            professional_id=professional.id,
            payer_name="Responsável",
            payment_date=date(2026, 9, 16),
            amount_cents=10_000,
            status="confirmed",
            receipt_number="REC-WEEKLY-1",
        )
    )
    payable = Payable(
        professional_id=professional.id,
        description="Aluguel",
        issue_date=week_start,
        competence_date=week_start,
        due_date=date(2026, 9, 17),
        total_cents=2_500,
        status="paid",
    )
    db_session.add(payable)
    await db_session.flush()
    db_session.add(
        PayableSettlement(
            payable_id=payable.id,
            professional_id=professional.id,
            payment_date=date(2026, 9, 17),
            amount_cents=2_500,
            status="confirmed",
        )
    )
    db_session.add(
        Receivable(
            professional_id=professional.id,
            payer_name="Responsável",
            description="Parcela vencida",
            issue_date=date(2026, 9, 1),
            competence_date=date(2026, 9, 1),
            due_date=date(2026, 9, 10),
            total_cents=7_000,
            status="open",
        )
    )
    await db_session.commit()

    sent: list[dict] = []

    def fake_send_email(**kwargs):
        sent.append(kwargs)
        return "resend-weekly-1"

    monkeypatch.setattr(
        "app.services.weekly_summary_email_service.send_email", fake_send_email
    )
    now = datetime(2026, 9, 19, 22, tzinfo=UTC)  # sábado 19h em São Paulo

    assert await run_weekly_summary_emails(db_session, now=now) == 1
    assert await run_weekly_summary_emails(db_session, now=now) == 0
    assert len(sent) == 1
    assert "João Silva" not in sent[0]["html"]
    assert "Paciente Demonstração" not in sent[0]["html"]
    assert "Taxa de comparecimento: 50%" in sent[0]["text"]
    assert "Ver agenda:" in sent[0]["text"]
    assert "endereço" not in sent[0]["html"].lower()
    assert sent[0]["idempotency_key"].startswith("weekly-summary/")
    assert "List-Unsubscribe" in sent[0]["headers"]

    delivery = await db_session.scalar(select(WeeklySummaryEmailDelivery))
    assert delivery.status == "sent"
    assert delivery.provider_message_id == "resend-weekly-1"
    assert delivery.snapshot == {
        "appointments": {
            "total": 6,
            "completed": 1,
            "noShow": 1,
            "cancelled": 1,
            "unfinished": 3,
            "attendanceRate": 50.0,
        },
        "finance": {
            "receivedCents": 10_000,
            "paidExpensesCents": 2_500,
            "balanceCents": 7_500,
            "overdueCount": 1,
            "overdueBalanceCents": 7_000,
        },
    }


async def test_signed_resend_webhook_suppresses_only_weekly_summary(
    api_client, db_session, professional, monkeypatch
):
    secret = "whsec_" + base64.b64encode(b"weekly-webhook-secret").decode()
    monkeypatch.setattr(get_settings(), "resend_webhook_secret", secret)
    notification_settings = NotificationSettings(
        professional_id=professional.id,
        birthday_in_app_enabled=True,
        weekly_summary_email_enabled=True,
    )
    delivery = WeeklySummaryEmailDelivery(
        professional_id=professional.id,
        week_start=date(2026, 9, 14),
        week_end=date(2026, 9, 19),
        status="sent",
        snapshot={},
        provider_message_id="resend-weekly-1",
    )
    db_session.add_all([notification_settings, delivery])
    await db_session.commit()

    payload = {
        "type": "email.bounced",
        "data": {
            "email_id": "resend-weekly-1",
            "bounce": {"type": "Permanent"},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    message_id = "msg_weekly_1"
    timestamp = str(int(datetime.now(UTC).timestamp()))
    signed = f"{message_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(
        hmac.new(b"weekly-webhook-secret", signed, hashlib.sha256).digest()
    ).decode()

    invalid = await api_client.post(
        "/api/v1/webhooks/resend/email",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": message_id,
            "svix-timestamp": timestamp,
            "svix-signature": "v1,invalid",
        },
    )
    assert invalid.status_code == 400
    await db_session.refresh(notification_settings)
    assert notification_settings.weekly_summary_email_enabled is True

    response = await api_client.post(
        "/api/v1/webhooks/resend/email",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": message_id,
            "svix-timestamp": timestamp,
            "svix-signature": f"v1,{signature}",
        },
    )
    assert response.status_code == 204
    await db_session.refresh(notification_settings)
    await db_session.refresh(delivery)
    assert notification_settings.weekly_summary_email_enabled is False
    assert notification_settings.weekly_summary_email_suppression_reason == "permanent_bounce"
    assert notification_settings.birthday_in_app_enabled is True
    assert delivery.status == "bounced"


async def test_weekly_job_records_empty_week_without_sending(
    db_session, professional, monkeypatch
):
    professional.subscription_status = "trialing"
    professional.trial_ends_at = datetime(2026, 9, 30, tzinfo=UTC)
    db_session.add(
        NotificationSettings(
            professional_id=professional.id,
            weekly_summary_email_enabled=True,
            weekly_summary_email_opted_in_at=datetime(2026, 9, 18, tzinfo=UTC),
        )
    )
    await db_session.commit()

    def unexpected_send(**_kwargs):
        raise AssertionError("não deve enviar")

    monkeypatch.setattr(
        "app.services.weekly_summary_email_service.send_email",
        unexpected_send,
    )

    result = await run_weekly_summary_emails(
        db_session, now=datetime(2026, 9, 19, 22, tzinfo=UTC)
    )

    assert result == 0
    delivery = await db_session.scalar(select(WeeklySummaryEmailDelivery))
    assert delivery.status == "skipped"
    assert delivery.skip_reason == "no_activity"


async def test_weekly_job_retries_with_the_same_provider_idempotency_key(
    db_session, professional, patient, monkeypatch
):
    professional.subscription_status = "trialing"
    professional.trial_ends_at = datetime(2026, 9, 30, tzinfo=UTC)
    db_session.add_all(
        [
            NotificationSettings(
                professional_id=professional.id,
                weekly_summary_email_enabled=True,
                weekly_summary_email_opted_in_at=datetime(2026, 9, 18, tzinfo=UTC),
            ),
            Appointment(
                professional_id=professional.id,
                patient_id=patient.id,
                date=date(2026, 9, 19),
                time=time(18),
                type="Fonoaudiologia",
                duration=50,
                status="concluido",
            ),
        ]
    )
    await db_session.commit()
    keys = []

    def flaky_send_email(**kwargs):
        keys.append(kwargs["idempotency_key"])
        if len(keys) == 1:
            raise RuntimeError("provider unavailable")
        return "resend-retry-1"

    monkeypatch.setattr(
        "app.services.weekly_summary_email_service.send_email", flaky_send_email
    )
    closing = datetime(2026, 9, 19, 22, tzinfo=UTC)

    assert await run_weekly_summary_emails(db_session, now=closing) == 0
    assert await run_weekly_summary_emails(
        db_session, now=closing + timedelta(minutes=14)
    ) == 0
    assert await run_weekly_summary_emails(
        db_session, now=closing + timedelta(minutes=15)
    ) == 1
    assert len(keys) == 2
    assert keys[0] == keys[1]
    delivery = await db_session.scalar(select(WeeklySummaryEmailDelivery))
    assert delivery.status == "sent"
    assert delivery.attempt_count == 2
