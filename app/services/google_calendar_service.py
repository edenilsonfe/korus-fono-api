from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.appointment import Appointment
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarSyncRecord
from app.models.patient import Patient

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
OAUTH_STATE_TYPE = "google_calendar_oauth"


class GoogleCalendarError(RuntimeError):
    pass


def _clinic_timezone():
    name = get_settings().clinic_timezone
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/Sao_Paulo":
            return timezone(timedelta(hours=-3), name=name)
        raise GoogleCalendarError(f"Fuso horário da clínica não disponível: {name}")


def _fernet() -> Fernet:
    key = get_settings().google_calendar_credential_encryption_key.strip()
    if not key:
        raise GoogleCalendarError("A criptografia da integração Google não está configurada.")
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise GoogleCalendarError("A chave de criptografia da integração Google é inválida.") from exc


def encrypt_refresh_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_refresh_token(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise GoogleCalendarError("Não foi possível ler a credencial salva do Google.") from exc


def create_oauth_state(professional_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(professional_id),
            "type": OAUTH_STATE_TYPE,
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_oauth_state(state: str) -> UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(
            state,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "type", "iat", "exp"]},
        )
        if payload.get("type") != OAUTH_STATE_TYPE:
            raise ValueError("wrong state type")
        return UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise GoogleCalendarError("A autorização do Google expirou ou é inválida.") from exc


def build_authorization_url(professional_id: UUID) -> str:
    settings = get_settings()
    if not settings.google_calendar_configured:
        raise GoogleCalendarError("A integração com Google Agenda ainda não foi configurada.")
    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": settings.google_calendar_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": create_oauth_state(professional_id),
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


async def exchange_authorization_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "redirect_uri": settings.google_calendar_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        raise GoogleCalendarError("O Google recusou a troca do código de autorização.")
    payload = response.json()
    granted = set(str(payload.get("scope", "")).split())
    if granted and GOOGLE_CALENDAR_SCOPE not in granted:
        raise GoogleCalendarError("A permissão necessária do Google Agenda não foi concedida.")
    return payload


async def save_connection(db: AsyncSession, professional_id: UUID, token_payload: dict) -> None:
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.professional_id == professional_id
            )
        )
    ).scalar_one_or_none()
    refresh_token = token_payload.get("refresh_token")
    if connection is None and not refresh_token:
        raise GoogleCalendarError("O Google não devolveu acesso offline. Tente conectar novamente.")
    if connection is None:
        connection = GoogleCalendarConnection(
            professional_id=professional_id,
            encrypted_refresh_token=encrypt_refresh_token(str(refresh_token)),
            connected_at=datetime.now(UTC),
        )
        db.add(connection)
    else:
        if refresh_token:
            connection.encrypted_refresh_token = encrypt_refresh_token(str(refresh_token))
        connection.connected_at = datetime.now(UTC)
        connection.last_error = None
    await db.commit()


async def _access_token(connection: GoogleCalendarConnection) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "refresh_token": decrypt_refresh_token(connection.encrypted_refresh_token),
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        raise GoogleCalendarError("A autorização do Google expirou. Conecte a conta novamente.")
    token = response.json().get("access_token")
    if not token:
        raise GoogleCalendarError("O Google não devolveu uma credencial de acesso válida.")
    return str(token)


def appointment_snapshot(appointment: Appointment, patient_name: str) -> dict:
    return {
        "appointment_id": str(appointment.id),
        "date": appointment.date.isoformat(),
        "time": appointment.time.isoformat(),
        "duration": appointment.duration,
        "appointment_type": appointment.type,
        "status": appointment.status,
        "patient_name": patient_name,
    }


async def queue_appointment_sync(
    db: AsyncSession,
    appointment: Appointment,
    patient_name: str,
    *,
    operation: str | None = None,
) -> GoogleCalendarSyncRecord | None:
    connection = (
        await db.execute(
            select(GoogleCalendarConnection.id).where(
                GoogleCalendarConnection.professional_id == appointment.professional_id
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return None
    record = (
        await db.execute(
            select(GoogleCalendarSyncRecord).where(
                GoogleCalendarSyncRecord.appointment_id == appointment.id
            )
        )
    ).scalar_one_or_none()
    resolved_operation = operation or ("delete" if appointment.status == "cancelado" else "upsert")
    if record is None:
        record = GoogleCalendarSyncRecord(
            professional_id=appointment.professional_id,
            appointment_id=appointment.id,
        )
        db.add(record)
    record.operation = resolved_operation
    record.event_snapshot = appointment_snapshot(appointment, patient_name)
    record.status = "queued"
    record.attempt_count = 0
    record.last_error = None
    record.processed_at = None
    await db.flush()
    return record


async def queue_future_appointments(db: AsyncSession, professional_id: UUID) -> list[GoogleCalendarSyncRecord]:
    today = datetime.now(_clinic_timezone()).date()
    rows = (
        await db.execute(
            select(Appointment, Patient.name)
            .join(Patient, Patient.id == Appointment.patient_id)
            .where(
                Appointment.professional_id == professional_id,
                Appointment.date >= today,
            )
            .order_by(Appointment.date, Appointment.time)
        )
    ).all()
    records: list[GoogleCalendarSyncRecord] = []
    for appointment, patient_name in rows:
        record = await queue_appointment_sync(db, appointment, patient_name)
        if record:
            records.append(record)
    await db.commit()
    return records


def _event_body(snapshot: dict, *, include_patient_name: bool) -> dict:
    clinic_timezone = _clinic_timezone()
    start = datetime.combine(
        date.fromisoformat(snapshot["date"]),
        datetime.fromisoformat(f"2000-01-01T{snapshot['time']}").time(),
        tzinfo=clinic_timezone,
    )
    end = start + timedelta(minutes=int(snapshot["duration"]))
    summary = "Atendimento KorusFono"
    if include_patient_name:
        summary = f"Atendimento — {snapshot['patient_name']}"
    return {
        "summary": summary,
        "description": f"{snapshot['appointment_type']}\nGerenciado pelo KorusFono.",
        "start": {"dateTime": start.isoformat(), "timeZone": get_settings().clinic_timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": get_settings().clinic_timezone},
        "extendedProperties": {
            "private": {"korusAppointmentId": snapshot["appointment_id"]}
        },
    }


async def _google_request(
    token: str, method: str, path: str, *, json_body: dict | None = None
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=25) as client:
        return await client.request(
            method,
            f"https://www.googleapis.com/calendar/v3{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
        )


async def _find_existing_event(token: str, calendar_id: str, appointment_id: str) -> str | None:
    query = urlencode({"privateExtendedProperty": f"korusAppointmentId={appointment_id}", "maxResults": 1})
    response = await _google_request(
        token, "GET", f"/calendars/{quote(calendar_id, safe='')}/events?{query}"
    )
    if response.status_code >= 400:
        raise GoogleCalendarError("Não foi possível consultar os eventos no Google Agenda.")
    items = response.json().get("items") or []
    return str(items[0]["id"]) if items else None


async def dispatch_sync_record(record_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        record = await db.get(GoogleCalendarSyncRecord, record_id)
        if record is None or record.status not in {"queued", "failed", "processing"}:
            return
        connection = (
            await db.execute(
                select(GoogleCalendarConnection).where(
                    GoogleCalendarConnection.professional_id == record.professional_id
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            return
        record.status = "processing"
        record.attempt_count += 1
        await db.commit()
        try:
            token = await _access_token(connection)
            calendar_path = quote(connection.calendar_id, safe="")
            snapshot = record.event_snapshot or {}
            event_id = record.google_event_id
            if record.operation == "delete":
                if event_id:
                    response = await _google_request(
                        token,
                        "DELETE",
                        f"/calendars/{calendar_path}/events/{quote(event_id, safe='')}",
                    )
                    if response.status_code not in {204, 404, 410}:
                        raise GoogleCalendarError("Não foi possível remover o evento do Google Agenda.")
            else:
                if not event_id:
                    event_id = await _find_existing_event(
                        token, connection.calendar_id, snapshot["appointment_id"]
                    )
                body = _event_body(snapshot, include_patient_name=connection.include_patient_name)
                if event_id:
                    response = await _google_request(
                        token,
                        "PUT",
                        f"/calendars/{calendar_path}/events/{quote(event_id, safe='')}",
                        json_body=body,
                    )
                    if response.status_code == 404:
                        event_id = None
                if not event_id:
                    response = await _google_request(
                        token,
                        "POST",
                        f"/calendars/{calendar_path}/events",
                        json_body=body,
                    )
                if response.status_code >= 400:
                    raise GoogleCalendarError("Não foi possível salvar o evento no Google Agenda.")
                event_id = str(response.json().get("id") or event_id or "") or None
            record.google_event_id = event_id
            record.status = "synced"
            record.last_error = None
            record.processed_at = datetime.now(UTC)
            connection.last_sync_at = record.processed_at
            connection.last_error = None
        except Exception as exc:
            message = str(exc)[:500] if isinstance(exc, GoogleCalendarError) else "Falha temporária ao sincronizar com o Google Agenda."
            record.status = "failed"
            record.last_error = message
            connection.last_error = message
            logger.warning("Google Calendar sync failed for record %s: %s", record.id, type(exc).__name__)
        await db.commit()


async def dispatch_sync_records(record_ids: list[UUID]) -> None:
    for record_id in record_ids:
        await dispatch_sync_record(record_id)


async def retry_pending_syncs(_ctx=None) -> None:
    stale_processing = datetime.now(UTC) - timedelta(minutes=10)
    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.execute(
                    select(GoogleCalendarSyncRecord.id)
                    .where(
                        or_(
                            GoogleCalendarSyncRecord.status == "queued",
                            (
                                (GoogleCalendarSyncRecord.status == "failed")
                                & (GoogleCalendarSyncRecord.attempt_count < 5)
                            ),
                            (
                                (GoogleCalendarSyncRecord.status == "processing")
                                & (GoogleCalendarSyncRecord.updated_at < stale_processing)
                            ),
                        )
                    )
                    .order_by(GoogleCalendarSyncRecord.updated_at)
                    .limit(100)
                )
            ).scalars()
        )
    await dispatch_sync_records(ids)


async def status_counts(db: AsyncSession, professional_id: UUID) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(GoogleCalendarSyncRecord.status, func.count())
            .where(GoogleCalendarSyncRecord.professional_id == professional_id)
            .group_by(GoogleCalendarSyncRecord.status)
        )
    ).all()
    counts = dict(rows)
    return int(counts.get("queued", 0) + counts.get("processing", 0)), int(counts.get("failed", 0))


async def revoke_connection(connection: GoogleCalendarConnection) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": decrypt_refresh_token(connection.encrypted_refresh_token)},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception:
        logger.warning("Google OAuth revocation failed; removing local connection")
