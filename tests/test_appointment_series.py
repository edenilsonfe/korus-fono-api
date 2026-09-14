"""Tests for recurring appointment series slot generation."""

from datetime import date, time, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.db.session import get_db
from app.main import app
from app.models.appointment import Appointment
from app.services.appointment_series_slots import (
    MAX_RECURRENT_DAYS,
    AppointmentSlot,
    iter_recurring_child_slots,
    slot_matches,
    validate_recurrent_range,
)


def test_iter_recurring_child_slots_weekly_skips_anchor_day():
    start = date(2026, 1, 5)
    end = date(2026, 1, 19)
    slots = list(
        iter_recurring_child_slots(
            frequency="semanal",
            start_date=start,
            end_date=end,
            start_time=time(10, 0),
            end_time=time(11, 0),
            duration=60,
        )
    )
    assert slots == [
        AppointmentSlot(date(2026, 1, 12), time(10, 0), time(11, 0), 60),
        AppointmentSlot(date(2026, 1, 19), time(10, 0), time(11, 0), 60),
    ]


def test_slot_matches_exact_time():
    slot = AppointmentSlot(date(2026, 1, 12), time(10, 0), time(11, 0), 60)
    assert slot_matches(slot, date(2026, 1, 12), time(10, 0), time(11, 0))
    assert not slot_matches(slot, date(2026, 1, 12), time(10, 30), time(11, 0))


def test_validate_recurrent_range_rejects_over_max_days():
    start = date(2026, 1, 1)
    end = start + timedelta(days=MAX_RECURRENT_DAYS + 1)
    with pytest.raises(ValueError, match=str(MAX_RECURRENT_DAYS)):
        validate_recurrent_range(start, end)


def test_iter_recurring_child_slots_personalizado_weekdays():
    start = date(2026, 1, 5)  # Monday
    end = date(2026, 1, 18)
    slots = list(
        iter_recurring_child_slots(
            frequency="personalizado",
            start_date=start,
            end_date=end,
            start_time=time(10, 0),
            end_time=time(11, 0),
            weekdays=[0, 2],
            duration=60,
        )
    )
    assert slots == [
        AppointmentSlot(date(2026, 1, 7), time(10, 0), time(11, 0), 60),
        AppointmentSlot(date(2026, 1, 12), time(10, 0), time(11, 0), 60),
        AppointmentSlot(date(2026, 1, 14), time(10, 0), time(11, 0), 60),
    ]


def test_iter_recurring_child_slots_personalizado_per_weekday_times():
    from app.services.appointment_series_slots import WeekdaySlotRule

    start = date(2026, 1, 5)  # Monday
    end = date(2026, 1, 14)
    slots = list(
        iter_recurring_child_slots(
            frequency="personalizado",
            start_date=start,
            end_date=end,
            start_time=time(9, 0),
            end_time=time(9, 50),
            weekdays=[0, 2],
            duration=50,
            weekday_rules=[
                WeekdaySlotRule(weekday=0, start_time=time(9, 0), duration=50),
                WeekdaySlotRule(weekday=2, start_time=time(14, 0), duration=30),
            ],
        )
    )
    assert slots == [
        AppointmentSlot(date(2026, 1, 7), time(14, 0), time(14, 30), 30),
        AppointmentSlot(date(2026, 1, 12), time(9, 0), time(9, 50), 50),
        AppointmentSlot(date(2026, 1, 14), time(14, 0), time(14, 30), 30),
    ]


@pytest.mark.asyncio
async def test_create_personalizado_series(
    db_session, professional, patient, auth_headers,
):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    start = date(2026, 6, 1)  # Monday
    end = date(2026, 6, 14)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/appointments",
            headers=auth_headers,
            json={
                "patientId": str(patient.id),
                "date": start.isoformat(),
                "time": "10:00",
                "type": "Terapia individual",
                "duration": 60,
                "status": "confirmado",
                "appointmentType": "recorrente",
                "frequency": "personalizado",
                "endDate": end.isoformat(),
                "weekdays": [0, 2],
            },
        )

    app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["frequency"] == "personalizado"
    assert body["weekdays"] == [0, 2]


@pytest.mark.asyncio
async def test_create_personalizado_series_with_weekday_slots(
    db_session, professional, patient, auth_headers,
):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    start = date(2026, 6, 1)  # Monday
    end = date(2026, 6, 14)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/appointments",
            headers=auth_headers,
            json={
                "patientId": str(patient.id),
                "date": start.isoformat(),
                "time": "09:00",
                "type": "Terapia individual",
                "duration": 50,
                "status": "confirmado",
                "appointmentType": "recorrente",
                "frequency": "personalizado",
                "endDate": end.isoformat(),
                "weekdaySlots": [
                    {"weekday": 0, "time": "09:00", "duration": 50},
                    {"weekday": 2, "time": "14:00", "duration": 30},
                ],
            },
        )

    app.dependency_overrides.clear()

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["frequency"] == "personalizado"
    assert body["weekdays"] == [0, 2]
    slots = body["weekdaySlots"]
    assert len(slots) == 2
    assert slots[0]["weekday"] == 0 and slots[0]["duration"] == 50
    assert str(slots[0]["time"]).startswith("09:00")
    assert slots[1]["weekday"] == 2 and slots[1]["duration"] == 30
    assert str(slots[1]["time"]).startswith("14:00")

    result = await db_session.execute(
        select(Appointment).where(Appointment.patient_id == patient.id)
    )
    appointments = sorted(result.scalars().all(), key=lambda a: (a.date, a.time))
    by_date = {a.date: a for a in appointments}
    wed = by_date[date(2026, 6, 3)]
    assert wed.time == time(14, 0)
    assert wed.duration == 30
    mon2 = by_date[date(2026, 6, 8)]
    assert mon2.time == time(9, 0)
    assert mon2.duration == 50


@pytest.mark.asyncio
async def test_create_recurring_series_children_keep_recorrente_type(
    db_session, professional, patient, auth_headers,
):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    start = date(2026, 6, 1)
    end = date(2026, 6, 15)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/appointments",
            headers=auth_headers,
            json={
                "patientId": str(patient.id),
                "date": start.isoformat(),
                "time": "10:00",
                "type": "Terapia individual",
                "duration": 60,
                "status": "confirmado",
                "appointmentType": "recorrente",
                "frequency": "semanal",
                "endDate": end.isoformat(),
            },
        )

    app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["appointmentType"] == "recorrente"
    assert body["childrenCreated"] >= 1

    result = await db_session.execute(
        select(Appointment).where(Appointment.patient_id == patient.id)
    )
    appointments = result.scalars().all()
    assert len(appointments) >= 2
    assert all(a.appointment_type == "recorrente" for a in appointments)


@pytest.mark.asyncio
async def test_weekly_series_cannot_be_recreated_with_an_extra_day(
    api_client, patient, auth_headers, db_session,
):
    start = date.today() + timedelta(days=7)
    start += timedelta(days=(7 - start.weekday()) % 7)
    payload = {
        "patientId": str(patient.id), "date": start.isoformat(), "time": "10:00",
        "type": "Terapia individual", "duration": 50, "appointmentType": "recorrente",
        "frequency": "semanal", "endDate": (start + timedelta(days=20)).isoformat(),
    }
    first = await api_client.post("/api/v1/appointments", headers=auth_headers, json=payload)
    assert first.status_code == 201, first.text
    retry = await api_client.post("/api/v1/appointments", headers=auth_headers, json={
        **payload, "date": (start + timedelta(days=7)).isoformat(),
        "frequency": "personalizado", "weekdays": [0, 2],
    })
    assert retry.status_code == 409, retry.text
    assert "Conflito de horário" in retry.json()["detail"]
    rows = (await db_session.scalars(select(Appointment))).all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_edit_series_moves_future_days_without_recreating_occupied_slots(
    api_client, patient, auth_headers, db_session,
):
    start = date.today() + timedelta(days=7)
    start += timedelta(days=(7 - start.weekday()) % 7)
    first = await api_client.post("/api/v1/appointments", headers=auth_headers, json={
        "patientId": str(patient.id), "date": start.isoformat(), "time": "10:00",
        "type": "Terapia individual", "duration": 50, "appointmentType": "recorrente",
        "frequency": "semanal", "endDate": (start + timedelta(days=20)).isoformat(),
    })
    assert first.status_code == 201, first.text
    rows = (await db_session.scalars(select(Appointment).order_by(Appointment.date))).all()
    selected_id = rows[1].id
    result = await api_client.patch(f"/api/v1/appointments/{selected_id}/series", headers=auth_headers, json={
        "fromDate": (start + timedelta(days=7)).isoformat(),
        "endDate": (start + timedelta(days=20)).isoformat(),
        "frequency": "personalizado", "time": "10:00", "duration": 50,
        "weekdaySlots": [
            {"weekday": 1, "time": "09:00", "duration": 50},
            {"weekday": 3, "time": "14:00", "duration": 30},
        ],
    })
    assert result.status_code == 200, result.text
    rows = (await db_session.scalars(select(Appointment).order_by(Appointment.date))).all()
    active = [row for row in rows if row.status != "cancelado"]
    assert [(row.date, row.time, row.duration) for row in active] == [
        (start, time(10), 50),
        (start + timedelta(days=8), time(9), 50),
        (start + timedelta(days=10), time(14), 30),
        (start + timedelta(days=15), time(9), 50),
        (start + timedelta(days=17), time(14), 30),
    ]
    assert result.json()["createdCount"] == 2
    assert result.json()["updatedCount"] == 2
    assert result.json()["cancelledCount"] == 0
    assert next(row for row in active if row.id == selected_id).date == start + timedelta(days=8)


@pytest.mark.asyncio
async def test_series_edit_keeps_existing_ids_and_is_repeatable(
    api_client, patient, professional, auth_headers, db_session,
):
    start = date.today() + timedelta(days=7)
    anchor = Appointment(professional_id=professional.id, patient_id=patient.id,
        date=start, time=time(10), type="Terapia", duration=50, status="confirmado",
        appointment_type="recorrente", frequency="semanal", end_date=start + timedelta(days=14),
        service_name_snapshot="Preço combinado", service_price_cents=12500)
    db_session.add(anchor)
    await db_session.flush()
    child = Appointment(professional_id=professional.id, patient_id=patient.id,
        date=start + timedelta(days=7), time=time(10), type="Terapia", duration=50,
        status="confirmado", appointment_type="recorrente", series_id=anchor.id)
    db_session.add(child)
    await db_session.commit()
    original_ids = {anchor.id, child.id}
    payload = {"fromDate": start.isoformat(), "endDate": (start + timedelta(days=13)).isoformat(),
        "frequency": "personalizado", "time": "10:00", "duration": 50,
        "weekdaySlots": [{"weekday": start.weekday(), "time": "10:00", "duration": 50},
                         {"weekday": (start.weekday() + 2) % 7, "time": "14:00", "duration": 30}]}
    for _ in range(2):
        response = await api_client.patch(f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers, json=payload)
        assert response.status_code == 200, response.text
    rows = (await db_session.scalars(select(Appointment))).all()
    assert len(rows) == 4
    assert {row.id for row in rows if row.status == "confirmado"} == original_ids
    assert all(row.service_price_cents == 12500 and row.status == "pendente" for row in rows if row.id not in original_ids)
    assert response.json() == {"createdCount": 0, "updatedCount": 0, "cancelledCount": 0, "preservedCount": 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("obstacle", ["same_patient", "other_patient", "block"])
async def test_series_edit_conflict_rolls_back_the_entire_change(
    obstacle, api_client, patient, professional, auth_headers, db_session,
):
    from app.models.patient import Patient
    from app.models.schedule_block import ScheduleBlock
    start = date.today() + timedelta(days=7)
    anchor = Appointment(professional_id=professional.id, patient_id=patient.id,
        date=start, time=time(10), type="Terapia", duration=50, status="confirmado",
        appointment_type="recorrente", frequency="semanal")
    db_session.add(anchor)
    conflict_date = start + timedelta(days=7)
    if obstacle == "block":
        db_session.add(ScheduleBlock(professional_id=professional.id, start_date=conflict_date, end_date=conflict_date))
    else:
        conflict_patient = patient
        if obstacle == "other_patient":
            conflict_patient = Patient(professional_id=professional.id, name="Outro paciente",
                birth_date=patient.birth_date, status="ativo", start_date=start, avatar_color=patient.avatar_color)
            db_session.add(conflict_patient)
            await db_session.flush()
        db_session.add(Appointment(professional_id=professional.id, patient_id=conflict_patient.id,
            date=conflict_date, time=time(11), type="Terapia", duration=50, status="pendente"))
    await db_session.commit()
    before = [(a.id, a.date, a.time, a.status) for a in (await db_session.scalars(select(Appointment))).all()]
    response = await api_client.patch(f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers, json={
        "fromDate": start.isoformat(), "endDate": conflict_date.isoformat(),
        "frequency": "semanal", "time": "11:00", "duration": 50,
    })
    assert response.status_code == 409, response.text
    assert conflict_date.strftime("%d/%m/%Y") in response.json()["detail"]
    assert "Nenhum horário foi alterado" in response.json()["detail"]
    assert [(a.id, a.date, a.time, a.status) for a in (await db_session.scalars(select(Appointment))).all()] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["past", "concluido", "falta", "cancelado", "session", "finance"])
async def test_series_edit_preserves_protected_appointments(
    protection, api_client, patient, professional, auth_headers, db_session,
):
    from datetime import datetime, UTC
    from app.models.session import Session
    from app.models.finance import Receivable, ReceivableItem
    start = date.today() + timedelta(days=7)
    anchor = Appointment(professional_id=professional.id, patient_id=patient.id,
        date=start if protection != "past" else date.today() - timedelta(days=1), time=time(10),
        type="Terapia", duration=50, status=protection if protection in {"concluido", "falta", "cancelado"} else "confirmado",
        appointment_type="recorrente", frequency="semanal")
    db_session.add(anchor)
    await db_session.flush()
    if protection == "session":
        db_session.add(Session(professional_id=professional.id, patient_id=patient.id,
            appointment_id=anchor.id, date=datetime.now(UTC), duration=50, type="Terapia"))
    if protection == "finance":
        receivable = Receivable(professional_id=professional.id, patient_id=patient.id,
            payer_name="Responsável", description="Atendimento", issue_date=start,
            competence_date=start, due_date=start, total_cents=12500)
        db_session.add(receivable)
        await db_session.flush()
        db_session.add(ReceivableItem(receivable_id=receivable.id, appointment_id=anchor.id,
            description="Atendimento", unit_cents=12500, total_cents=12500))
    await db_session.commit()
    original = (anchor.date, anchor.time, anchor.status, anchor.frequency)
    response = await api_client.patch(f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers, json={
        "fromDate": start.isoformat(), "endDate": (start + timedelta(days=7)).isoformat(),
        "frequency": "semanal", "time": "10:00", "duration": 50,
    })
    assert response.status_code == 200, response.text
    await db_session.refresh(anchor)
    assert (anchor.date, anchor.time, anchor.status, anchor.frequency) == original
    assert response.json()["preservedCount"] == 1
    same_slot = (await db_session.scalars(select(Appointment).where(Appointment.date == anchor.date, Appointment.time == anchor.time))).all()
    assert len(same_slot) == 1


@pytest.mark.asyncio
async def test_series_edit_rejects_another_professional_and_invalid_days(
    api_client, patient, professional, auth_headers, db_session,
):
    from app.models.professional import Professional
    other = Professional(email="other-series@example.com", password_hash="test-only", name="Outra profissional")
    db_session.add(other)
    await db_session.flush()
    start = date.today() + timedelta(days=7)
    anchor = Appointment(professional_id=other.id, patient_id=patient.id, date=start,
        time=time(10), type="Terapia", duration=50, status="pendente")
    db_session.add(anchor)
    await db_session.commit()
    payload = {"fromDate": start.isoformat(), "endDate": start.isoformat(), "frequency": "semanal", "time": "10:00", "duration": 50}
    response = await api_client.patch(f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers, json=payload)
    assert response.status_code == 404
    response = await api_client.patch(f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers, json={
        **payload, "frequency": "personalizado", "weekdaySlots": [
            {"weekday": 1, "time": "10:00", "duration": 50},
            {"weekday": 1, "time": "11:00", "duration": 50},
        ],
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_series_edit_shortens_end_date_and_queues_notifications(
    api_client, patient, professional, auth_headers, db_session, monkeypatch,
):
    from unittest.mock import AsyncMock
    from app.models.notification_message_log import NotificationMessageLog
    from app.models.notification_settings import NotificationSettings

    enqueue = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.appointments.enqueue_whatsapp_appointment_event_log", enqueue,
    )
    start = date.today() + timedelta(days=7)
    anchor = Appointment(
        professional_id=professional.id, patient_id=patient.id,
        date=start, time=time(10), type="Terapia", duration=50, status="confirmado",
        appointment_type="recorrente", frequency="semanal", end_date=start + timedelta(days=14),
    )
    db_session.add(anchor)
    await db_session.flush()
    for days in (7, 14):
        db_session.add(Appointment(
            professional_id=professional.id, patient_id=patient.id,
            date=start + timedelta(days=days), time=time(10), type="Terapia", duration=50,
            status="pendente", appointment_type="recorrente", series_id=anchor.id,
        ))
    db_session.add(NotificationSettings(
        professional_id=professional.id, whatsapp_enabled=True,
        whatsapp_events={"appointment_rescheduled": True, "appointment_cancelled": True},
    ))
    await db_session.commit()

    response = await api_client.patch(
        f"/api/v1/appointments/{anchor.id}/series", headers=auth_headers,
        json={
            "fromDate": start.isoformat(), "endDate": (start + timedelta(days=7)).isoformat(),
            "frequency": "semanal", "time": "11:00", "duration": 50,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "createdCount": 0, "updatedCount": 2, "cancelledCount": 1, "preservedCount": 0,
    }
    rows = (await db_session.scalars(select(Appointment).order_by(Appointment.date))).all()
    assert [(row.time, row.status) for row in rows] == [
        (time(11), "confirmado"), (time(11), "pendente"), (time(10), "cancelado"),
    ]
    logs = (await db_session.scalars(select(NotificationMessageLog))).all()
    assert {(log.notification_type, log.scheduled_date, log.scheduled_time) for log in logs} == {
        ("appointment_rescheduled", start, time(11)),
        ("appointment_rescheduled", start + timedelta(days=7), time(11)),
        ("appointment_cancelled", start + timedelta(days=14), time(10)),
    }
    assert {call.args[0] for call in enqueue.await_args_list} == {log.id for log in logs}
