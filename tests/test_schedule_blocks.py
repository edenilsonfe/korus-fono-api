from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token, hash_password
from app.models.professional import Professional


@pytest.fixture(autouse=True)
async def _use_test_database_for_entitlement(monkeypatch, db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


def _appointment_body(patient_id: str, *, date: str, time: str) -> dict:
    return {
        "patientId": patient_id,
        "date": date,
        "time": time,
        "type": "Terapia individual",
        "duration": 50,
    }


@pytest.mark.asyncio
async def test_professional_creates_lists_and_deletes_full_day_date_range_block(
    api_client, auth_headers
):
    created = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-09-10",
            "endDate": "2026-09-12",
            "allDay": True,
            "reason": "Congresso",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["startDate"] == "2026-09-10"
    assert body["endDate"] == "2026-09-12"
    assert body["allDay"] is True
    assert body["startTime"] is None
    assert body["endTime"] is None
    assert body["reason"] == "Congresso"

    listed = await api_client.get(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        params={"from": "2026-09-11", "to": "2026-09-30"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    deleted = await api_client.delete(
        f"/api/v1/schedule-blocks/{body['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204

    empty = await api_client.get(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        params={"from": "2026-09-01", "to": "2026-09-30"},
    )
    assert empty.json() == []


@pytest.mark.asyncio
async def test_timed_block_prevents_new_and_rescheduled_appointments(
    api_client, auth_headers, patient
):
    existing = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-09-15", time="09:00"),
    )
    assert existing.status_code == 201

    block = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-09-15",
            "endDate": "2026-09-15",
            "allDay": False,
            "startTime": "13:00",
            "endTime": "15:00",
            "reason": "Reunião",
        },
    )
    assert block.status_code == 201

    conflicting_create = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-09-15", time="14:30"),
    )
    assert conflicting_create.status_code == 409
    assert conflicting_create.json()["detail"] == "Horário indisponível na agenda"

    conflicting_update = await api_client.patch(
        f"/api/v1/appointments/{existing.json()['id']}",
        headers=auth_headers,
        json={"time": "13:30"},
    )
    assert conflicting_update.status_code == 409
    assert conflicting_update.json()["detail"] == "Horário indisponível na agenda"

    ending_at_block_start = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-09-15", time="12:10"),
    )
    assert ending_at_block_start.status_code == 201

    starting_at_block_end = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-09-15", time="15:00"),
    )
    assert starting_at_block_end.status_code == 201


@pytest.mark.asyncio
async def test_full_day_block_rejects_every_time_on_each_date(
    api_client, auth_headers, patient
):
    block = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-10-01",
            "endDate": "2026-10-03",
            "allDay": True,
        },
    )
    assert block.status_code == 201

    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-10-02", time="08:00"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Horário indisponível na agenda"


@pytest.mark.asyncio
async def test_block_cannot_overlap_an_active_appointment_or_another_block(
    api_client, auth_headers, patient
):
    appointment = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json=_appointment_body(str(patient.id), date="2026-10-05", time="10:00"),
    )
    assert appointment.status_code == 201

    over_appointment = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-10-05",
            "endDate": "2026-10-05",
            "allDay": False,
            "startTime": "09:30",
            "endTime": "10:30",
        },
    )
    assert over_appointment.status_code == 409
    assert over_appointment.json()["detail"] == "Há agendamento no período informado"

    first = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-10-06",
            "endDate": "2026-10-06",
            "allDay": False,
            "startTime": "12:00",
            "endTime": "13:00",
        },
    )
    assert first.status_code == 201

    overlapping = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-10-06",
            "endDate": "2026-10-06",
            "allDay": False,
            "startTime": "12:30",
            "endTime": "14:00",
        },
    )
    assert overlapping.status_code == 409
    assert overlapping.json()["detail"] == "Já existe um bloqueio nesse período"

    adjacent = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-10-06",
            "endDate": "2026-10-06",
            "allDay": False,
            "startTime": "13:00",
            "endTime": "14:00",
        },
    )
    assert adjacent.status_code == 201


@pytest.mark.asyncio
async def test_recurring_series_is_rejected_when_a_later_occurrence_is_blocked(
    api_client, auth_headers, patient
):
    block = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-09-14",
            "endDate": "2026-09-14",
            "allDay": True,
        },
    )
    assert block.status_code == 201

    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            **_appointment_body(str(patient.id), date="2026-09-07", time="09:00"),
            "appointmentType": "recorrente",
            "frequency": "semanal",
            "endDate": "2026-09-21",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Horário indisponível na agenda"


@pytest.mark.asyncio
async def test_schedule_blocks_are_tenant_scoped(
    api_client, auth_headers, db_session
):
    created = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-11-01",
            "endDate": "2026-11-01",
            "allDay": True,
            "reason": "Bloqueio privado",
        },
    )
    assert created.status_code == 201

    other = Professional(
        email="other-schedule@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999999999",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    listed = await api_client.get(
        "/api/v1/schedule-blocks",
        headers=other_headers,
        params={"from": "2026-11-01", "to": "2026-11-30"},
    )
    assert listed.status_code == 200
    assert listed.json() == []

    deleted = await api_client.delete(
        f"/api/v1/schedule-blocks/{created.json()['id']}", headers=other_headers
    )
    assert deleted.status_code == 404


@pytest.mark.asyncio
async def test_schedule_block_validates_dates_and_times(api_client, auth_headers):
    invalid_range = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-12-02",
            "endDate": "2026-12-01",
            "allDay": True,
        },
    )
    assert invalid_range.status_code == 422

    invalid_time = await api_client.post(
        "/api/v1/schedule-blocks",
        headers=auth_headers,
        json={
            "startDate": "2026-12-01",
            "endDate": "2026-12-01",
            "allDay": False,
            "startTime": "15:00",
            "endTime": "14:00",
        },
    )
    assert invalid_time.status_code == 422
