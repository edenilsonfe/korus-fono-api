from datetime import date, time, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import pytest
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarSyncRecord
from app.models.appointment import Appointment
from app.services.google_calendar_service import (
    GOOGLE_CALENDAR_SCOPE,
    build_authorization_url,
    decode_oauth_state,
    decrypt_refresh_token,
    encrypt_refresh_token,
)


@pytest.fixture(autouse=True)
def google_settings(db_engine, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(
        settings,
        "google_calendar_credential_encryption_key",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(settings, "app_public_url", "https://api.example.com")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)
    monkeypatch.setattr("app.services.google_calendar_service.AsyncSessionLocal", factory)


def test_authorization_url_uses_owned_events_scope_and_signed_state(professional):
    url = build_authorization_url(professional.id)
    params = parse_qs(urlparse(url).query)

    assert params["scope"] == [GOOGLE_CALENDAR_SCOPE]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["redirect_uri"] == [
        "https://api.example.com/api/v1/google-calendar/oauth/callback"
    ]
    assert decode_oauth_state(params["state"][0]) == professional.id


@pytest.mark.asyncio
async def test_oauth_callback_encrypts_refresh_token(
    api_client, db_session, professional, monkeypatch
):
    exchange = AsyncMock(
        return_value={"refresh_token": "refresh-token-plain", "scope": GOOGLE_CALENDAR_SCOPE}
    )
    monkeypatch.setattr("app.api.v1.google_calendar.exchange_authorization_code", exchange)
    state = parse_qs(urlparse(build_authorization_url(professional.id)).query)["state"][0]

    response = await api_client.get(
        "/api/v1/google-calendar/oauth/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://app.example.com/configuracoes?googleCalendar=connected"
    )
    connection = (
        await db_session.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.professional_id == professional.id
            )
        )
    ).scalar_one()
    assert connection.encrypted_refresh_token != "refresh-token-plain"
    assert decrypt_refresh_token(connection.encrypted_refresh_token) == "refresh-token-plain"


@pytest.mark.asyncio
async def test_appointment_create_queues_google_sync_without_sending_token_to_client(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    db_session.add(
        GoogleCalendarConnection(
            professional_id=professional.id,
            encrypted_refresh_token=encrypt_refresh_token("refresh-token"),
            connected_at=professional.created_at,
        )
    )
    await db_session.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr("app.api.v1.appointments.dispatch_sync_records", dispatch)

    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": (date.today() + timedelta(days=2)).isoformat(),
            "time": "10:00",
            "type": "Terapia individual",
            "duration": 50,
        },
    )

    assert response.status_code == 201
    assert "refresh" not in response.text.lower()
    record = (
        await db_session.execute(select(GoogleCalendarSyncRecord))
    ).scalar_one()
    assert record.operation == "upsert"
    assert record.event_snapshot["appointment_id"] == response.json()["id"]
    assert record.event_snapshot["patient_name"] == patient.name
    dispatch.assert_awaited_once_with([record.id])


@pytest.mark.asyncio
async def test_status_never_exposes_oauth_credentials(
    api_client, auth_headers, db_session, professional
):
    db_session.add(
        GoogleCalendarConnection(
            professional_id=professional.id,
            encrypted_refresh_token=encrypt_refresh_token("highly-secret-refresh-token"),
            connected_at=professional.created_at,
        )
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/google-calendar/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert "token" not in response.text.lower()
    assert "secret" not in response.text.lower()


@pytest.mark.asyncio
async def test_dispatch_hides_patient_name_by_default(
    db_session, professional, patient, monkeypatch
):
    connection = GoogleCalendarConnection(
        professional_id=professional.id,
        encrypted_refresh_token=encrypt_refresh_token("refresh-token"),
        connected_at=professional.created_at,
        include_patient_name=False,
    )
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(9, 0),
        type="Terapia individual",
        duration=50,
        status="pendente",
    )
    db_session.add_all([connection, appointment])
    await db_session.flush()
    record = GoogleCalendarSyncRecord(
        professional_id=professional.id,
        appointment_id=appointment.id,
        event_snapshot={
            "appointment_id": str(appointment.id),
            "date": appointment.date.isoformat(),
            "time": appointment.time.isoformat(),
            "duration": 50,
            "appointment_type": appointment.type,
            "status": appointment.status,
            "patient_name": patient.name,
        },
        operation="upsert",
        status="queued",
    )
    db_session.add(record)
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.google_calendar_service._access_token",
        AsyncMock(return_value="access-token"),
    )
    monkeypatch.setattr(
        "app.services.google_calendar_service._find_existing_event",
        AsyncMock(return_value=None),
    )
    google_request = AsyncMock(
        return_value=httpx.Response(200, json={"id": "google-event-id"})
    )
    monkeypatch.setattr(
        "app.services.google_calendar_service._google_request", google_request
    )
    from app.services.google_calendar_service import dispatch_sync_record

    await dispatch_sync_record(record.id)

    await db_session.refresh(record)
    assert record.status == "synced"
    assert record.google_event_id == "google-event-id"
    body = google_request.await_args.kwargs["json_body"]
    assert body["summary"] == "Atendimento KorusFono"
    assert patient.name not in str(body)


@pytest.mark.asyncio
async def test_patient_delete_queues_google_event_removal(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    connection = GoogleCalendarConnection(
        professional_id=professional.id,
        encrypted_refresh_token=encrypt_refresh_token("refresh-token"),
        connected_at=professional.created_at,
    )
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today() + timedelta(days=1),
        time=time(9, 0),
        type="Terapia individual",
        duration=50,
        status="pendente",
    )
    db_session.add_all([connection, appointment])
    await db_session.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr("app.api.v1.patients.dispatch_sync_records", dispatch)

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}", headers=auth_headers
    )

    assert response.status_code == 204
    record = (
        await db_session.execute(select(GoogleCalendarSyncRecord))
    ).scalar_one()
    assert record.operation == "delete"
    dispatch.assert_awaited_once_with([record.id])
