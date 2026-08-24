from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.services.patient_appointment_service import appointment_occurs_in_future


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)
    monkeypatch.setattr(
        "app.services.patient_appointment_service.ZoneInfo",
        lambda _key: timezone(timedelta(hours=-3)),
    )


def test_future_appointment_uses_clinic_local_date_and_time():
    clinic_now = datetime(2026, 8, 24, 14, 30, tzinfo=timezone(timedelta(hours=-3)))
    earlier_today = Appointment(date=date(2026, 8, 24), time=time(14, 0))
    later_today = Appointment(date=date(2026, 8, 24), time=time(15, 0))

    assert appointment_occurs_in_future(earlier_today, clinic_now) is False
    assert appointment_occurs_in_future(later_today, clinic_now) is True


@pytest.mark.asyncio
async def test_bulk_cancel_changes_only_cancellable_future_appointments(
    api_client, db_session, professional, patient, auth_headers
):
    yesterday = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() - timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="pendente",
    )
    future_pending = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="pendente",
    )
    future_confirmed = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=2),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="confirmado",
    )
    future_terminal = [
        Appointment(
            professional_id=professional.id,
            patient_id=patient.id,
            date=date.today() + timedelta(days=3 + index),
            time=time(10, 0),
            type="Terapia",
            duration=50,
            status=appointment_status,
        )
        for index, appointment_status in enumerate(("cancelado", "concluido", "falta"))
    ]
    db_session.add_all([yesterday, future_pending, future_confirmed, *future_terminal])
    await db_session.commit()

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/appointments/cancel-future",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"cancelledCount": 2}
    await db_session.refresh(yesterday)
    await db_session.refresh(future_pending)
    await db_session.refresh(future_confirmed)
    for appointment in future_terminal:
        await db_session.refresh(appointment)
    assert yesterday.status == "pendente"
    assert future_pending.status == "cancelado"
    assert future_confirmed.status == "cancelado"
    assert [appointment.status for appointment in future_terminal] == [
        "cancelado",
        "concluido",
        "falta",
    ]

    repeated = await api_client.patch(
        f"/api/v1/patients/{patient.id}/appointments/cancel-future",
        headers=auth_headers,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"cancelledCount": 0}


@pytest.mark.asyncio
async def test_bulk_cancel_persists_cancellation_outbox_before_dispatch(
    api_client, db_session, professional, patient, auth_headers, monkeypatch
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(10, 0),
        type="Terapia",
        duration=50,
        status="confirmado",
    )
    db_session.add_all(
        [
            appointment,
            NotificationSettings(
                professional_id=professional.id,
                whatsapp_enabled=True,
                whatsapp_events={"appointment_cancelled": True},
            ),
        ]
    )
    await db_session.commit()
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.patients.enqueue_whatsapp_appointment_event_log",
        enqueue,
    )

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/appointments/cancel-future",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    event_log = (
        await db_session.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.appointment_id == appointment.id,
                NotificationMessageLog.notification_type == "appointment_cancelled",
            )
        )
    ).scalar_one()
    assert event_log.payload["event_snapshot"]["appointment_status"] == "cancelado"
    enqueue.assert_awaited_once_with(event_log.id)
