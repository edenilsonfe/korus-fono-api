"""Appointment mutations persist WhatsApp events before background dispatch."""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.caregiver import Caregiver
from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_create_confirmed_appointment_persists_outbox_before_dispatch(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    caregiver = (
        await db_session.execute(select(Caregiver).where(Caregiver.patient_id == patient.id))
    ).scalar_one()
    caregiver.whatsapp_opt_in = True
    db_session.add(
        NotificationSettings(
            professional_id=professional.id,
            whatsapp_enabled=True,
            whatsapp_events={"appointment_confirmation": True},
        )
    )
    await db_session.commit()

    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.appointments.enqueue_whatsapp_appointment_event_log", enqueue
    )
    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": (date.today() + timedelta(days=2)).isoformat(),
            "time": "10:00",
            "type": "Terapia individual",
            "duration": 50,
            "status": "confirmado",
        },
    )

    assert response.status_code == 201
    event_log = (
        await db_session.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.notification_type == "appointment_confirmation"
            )
        )
    ).scalar_one()
    assert event_log.status == "queued"
    assert event_log.attempt_count == 0
    assert event_log.payload["event_snapshot"]["appointment_status"] == "confirmado"
    enqueue.assert_awaited_once_with(event_log.id)


@pytest.mark.asyncio
async def test_create_pending_appointment_does_not_queue_confirmation(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    db_session.add(
        NotificationSettings(
            professional_id=professional.id,
            whatsapp_enabled=True,
            whatsapp_events={"appointment_confirmation": True},
        )
    )
    await db_session.commit()
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.appointments.enqueue_whatsapp_appointment_event_log", enqueue
    )

    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": (date.today() + timedelta(days=3)).isoformat(),
            "time": "11:00",
            "type": "Terapia individual",
            "duration": 50,
            "status": "pendente",
        },
    )

    assert response.status_code == 201
    count = len(
        (
            await db_session.execute(select(NotificationMessageLog))
        ).scalars().all()
    )
    assert count == 0
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_reschedule_persists_new_slot_snapshot_before_dispatch(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=4),
        time=datetime.strptime("09:00", "%H:%M").time(),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add_all(
        [
            appointment,
            NotificationSettings(
                professional_id=professional.id,
                whatsapp_enabled=True,
                whatsapp_events={"appointment_rescheduled": True},
            ),
        ]
    )
    await db_session.commit()
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.appointments.enqueue_whatsapp_appointment_event_log", enqueue
    )

    response = await api_client.patch(
        f"/api/v1/appointments/{appointment.id}",
        headers=auth_headers,
        json={"time": "10:30"},
    )

    assert response.status_code == 200
    event_log = (
        await db_session.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.notification_type == "appointment_rescheduled"
            )
        )
    ).scalar_one()
    assert event_log.status == "queued"
    assert event_log.scheduled_time.strftime("%H:%M") == "10:30"
    assert event_log.payload["event_snapshot"]["scheduled_time"] == "10:30:00"
    enqueue.assert_awaited_once_with(event_log.id)
