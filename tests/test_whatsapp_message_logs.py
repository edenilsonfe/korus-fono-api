"""Auditable WhatsApp message history contract."""

from datetime import UTC, datetime, time, timedelta

import pytest

from app.models.notification_message_log import NotificationMessageLog
from app.services.whatsapp_message_log_service import WhatsAppMessageLogService


@pytest.mark.asyncio
async def test_message_log_list_exposes_delivery_audit_fields(
    db_session, professional, patient
):
    log = NotificationMessageLog(
        professional_id=professional.id,
        appointment_id=None,
        patient_id=patient.id,
        channel="whatsapp",
        notification_type="appointment_rescheduled",
        provider="evolution",
        provider_message_id="provider-123",
        deduplication_key="audit-row",
        status="superseded",
        scheduled_date=datetime.now(UTC).date(),
        scheduled_time=time(14, 30),
        attempt_count=2,
        payload={
            "skip_reason": "appointment_schedule_changed",
            "dispatch_decision": {"whatsapp_enabled": True},
        },
    )
    db_session.add(log)
    await db_session.commit()

    result = await WhatsAppMessageLogService(db_session).list_logs(
        professional.id, days=30
    )

    item = result["items"][0]
    assert item["scheduled_date"] == log.scheduled_date
    assert item["scheduled_time"] == log.scheduled_time
    assert item["attempt_count"] == 2
    assert item["provider_message_id"] == "provider-123"
    assert item["skip_reason"] == "appointment_schedule_changed"
    assert item["dispatch_decision"] == {"whatsapp_enabled": True}


@pytest.mark.asyncio
async def test_stats_do_not_count_queued_as_sent(db_session, professional):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            NotificationMessageLog(
                professional_id=professional.id,
                channel="whatsapp",
                notification_type="appointment_confirmation",
                provider="evolution",
                deduplication_key="stats-queued",
                status="queued",
                attempt_count=0,
                created_at=now - timedelta(minutes=2),
            ),
            NotificationMessageLog(
                professional_id=professional.id,
                channel="whatsapp",
                notification_type="appointment_confirmation",
                provider="evolution",
                deduplication_key="stats-superseded",
                status="superseded",
                attempt_count=1,
                created_at=now - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.commit()

    stats = await WhatsAppMessageLogService(db_session).get_stats(
        professional.id, days=30
    )

    assert stats["sent"] == 0
    assert stats["queued"] == 1
    assert stats["skipped"] == 1
