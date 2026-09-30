from datetime import date, time, timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarOAuthRequest, GoogleCalendarSyncRecord
from app.core.auth_cookies import GOOGLE_OAUTH_COOKIE, GOOGLE_OAUTH_COOKIE_PATH
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
    request_id = uuid4()
    url = build_authorization_url(professional.id, "initiating-browser", request_id, professional.token_version)
    params = parse_qs(urlparse(url).query)

    assert params["scope"] == [GOOGLE_CALENDAR_SCOPE]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["redirect_uri"] == [
        "https://app.example.com/api/v1/google-calendar/oauth/callback"
    ]
    assert decode_oauth_state(params["state"][0], "initiating-browser") == (
        professional.id, request_id, professional.token_version
    )


@pytest.mark.asyncio
async def test_oauth_callback_encrypts_refresh_token(
    api_client, auth_headers, db_session, professional, monkeypatch
):
    exchange = AsyncMock(
        return_value={"refresh_token": "refresh-token-plain", "scope": GOOGLE_CALENDAR_SCOPE}
    )
    monkeypatch.setattr("app.api.v1.google_calendar.exchange_authorization_code", exchange)
    authorization = await api_client.post("/api/v1/google-calendar/oauth/authorize", headers=auth_headers)
    assert authorization.status_code == 200
    state = parse_qs(urlparse(authorization.json()["authorizationUrl"]).query)["state"][0]
    browser_nonce = api_client.cookies.get(GOOGLE_OAUTH_COOKIE)
    cookie = authorization.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=600" in cookie

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
    assert api_client.cookies.get(GOOGLE_OAUTH_COOKIE) is None
    assert await db_session.scalar(select(GoogleCalendarOAuthRequest.id)) is None
    # Mesmo com o cookie antigo e outro código, a transação já foi consumida.
    api_client.cookies.set(GOOGLE_OAUTH_COOKIE, browser_nonce, path=GOOGLE_OAUTH_COOKIE_PATH)
    replay = await api_client.get("/api/v1/google-calendar/oauth/callback",
                                  params={"code": "another-code", "state": state})
    assert replay.headers["location"].endswith("googleCalendar=error")
    exchange.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["other-browser", "missing-cookie", "expired", "revoked", "disabled", "unverified"])
async def test_oauth_rejects_invalid_transaction_before_exchange(
    api_client, auth_headers, db_session, professional, monkeypatch, invalid,
):
    from datetime import UTC, datetime
    exchange = AsyncMock()
    monkeypatch.setattr("app.api.v1.google_calendar.exchange_authorization_code", exchange)
    authorization = await api_client.post("/api/v1/google-calendar/oauth/authorize", headers=auth_headers)
    assert authorization.status_code == 200
    state = parse_qs(urlparse(authorization.json()["authorizationUrl"]).query)["state"][0]
    if invalid in {"missing-cookie", "other-browser"}:
        api_client.cookies.clear()
        if invalid == "other-browser":
            api_client.cookies.set(GOOGLE_OAUTH_COOKIE, "another-browser", path=GOOGLE_OAUTH_COOKIE_PATH)
    elif invalid == "expired":
        transaction = (await db_session.execute(select(GoogleCalendarOAuthRequest))).scalar_one()
        transaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "revoked":
        professional.token_version += 1
    elif invalid == "disabled":
        professional.is_disabled = True
    else:
        professional.email_verified_at = None
    await db_session.commit()
    response = await api_client.get("/api/v1/google-calendar/oauth/callback",
                                    params={"code": "code", "state": state})
    assert response.headers["location"].endswith("googleCalendar=error")
    exchange.assert_not_awaited()
    assert await db_session.scalar(select(GoogleCalendarConnection.id)) is None


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


@pytest.mark.parametrize("create_result", ["timeout", "conflict"])
async def test_uncertain_google_create_reuses_persisted_event_id(
    db_session, professional, patient, monkeypatch, create_result,
):
    from app.services import google_calendar_service as service
    appointment = Appointment(professional_id=professional.id, patient_id=patient.id,
                              date=date.today(), time=time(9), type="Terapia", duration=50, status="pendente")
    db_session.add_all([appointment, GoogleCalendarConnection(professional_id=professional.id,
                       encrypted_refresh_token="mocked", connected_at=professional.created_at)])
    await db_session.flush()
    record = await service.queue_appointment_sync(db_session, appointment, patient.name)
    await db_session.commit()
    monkeypatch.setattr(service, "_access_token", AsyncMock(return_value="mocked"))
    monkeypatch.setattr(service, "_find_existing_event", AsyncMock(return_value=None))
    created = []

    async def request(_token, method, path, *, json_body=None):
        if method == "POST":
            created.append(json_body["id"])
            if create_result == "conflict":
                return httpx.Response(409)
            raise httpx.ReadTimeout("response lost after remote creation")
        assert method == "PUT" and path.endswith(created[0])
        return httpx.Response(200, json={"id": created[0]})

    monkeypatch.setattr(service, "_google_request", request)
    await service.dispatch_sync_record(record.id)
    await db_session.refresh(record)
    assert record.status == ("failed" if create_result == "timeout" else "synced")
    assert record.google_event_id == created[0]
    await service.dispatch_sync_record(record.id)
    await db_session.refresh(record)
    assert len(created) == 1 and record.status == "synced"


async def test_oauth_invalidation_during_exchange_prevents_credential_persistence(
    api_client, auth_headers, db_session, professional, monkeypatch,
):
    async def exchange(_code):
        professional.token_version += 1
        await db_session.commit()
        return {"refresh_token": "must-not-be-persisted"}

    monkeypatch.setattr("app.api.v1.google_calendar.exchange_authorization_code", exchange)
    authorization = await api_client.post("/api/v1/google-calendar/oauth/authorize", headers=auth_headers)
    state = parse_qs(urlparse(authorization.json()["authorizationUrl"]).query)["state"][0]
    response = await api_client.get("/api/v1/google-calendar/oauth/callback",
                                    params={"code": "code", "state": state})
    assert response.headers["location"].endswith("googleCalendar=error")
    assert await db_session.scalar(select(GoogleCalendarConnection.id)) is None
    assert await db_session.scalar(select(GoogleCalendarOAuthRequest.id)) is None
