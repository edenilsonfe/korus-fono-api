from __future__ import annotations

import logging
import hashlib
import hmac
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.appointment import Appointment
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarOAuthRequest, GoogleCalendarSyncRecord
from app.models.patient import Patient
from app.models.professional import Professional

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


def create_oauth_state(professional_id: UUID, browser_nonce: str, request_id: UUID, token_version: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(professional_id),
            "type": OAUTH_STATE_TYPE,
            "jti": str(request_id),
            "version": token_version,
            "browser_hash": hashlib.sha256(browser_nonce.encode()).hexdigest(),
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_oauth_state(state: str, browser_nonce: str) -> tuple[UUID, UUID, int]:
    settings = get_settings()
    try:
        payload = jwt.decode(
            state,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "type", "iat", "exp", "jti", "version", "browser_hash"]},
        )
        if payload.get("type") != OAUTH_STATE_TYPE:
            raise ValueError("wrong state type")
        if not browser_nonce or not hmac.compare_digest(
            payload["browser_hash"], hashlib.sha256(browser_nonce.encode()).hexdigest()
        ):
            raise ValueError("wrong initiating browser")
        if not isinstance(payload["version"], int):
            raise ValueError("wrong session version")
        return UUID(payload["sub"]), UUID(payload["jti"]), payload["version"]
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise GoogleCalendarError("A autorização do Google expirou ou é inválida.") from exc


def build_authorization_url(professional_id: UUID, browser_nonce: str, request_id: UUID, token_version: int) -> str:
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
        "state": create_oauth_state(professional_id, browser_nonce, request_id, token_version),
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


async def begin_authorization(db: AsyncSession, professional: Professional, browser_nonce: str) -> str:
    request_id = uuid4()
    url = build_authorization_url(professional.id, browser_nonce, request_id, professional.token_version)
    now = datetime.now(UTC)
    await db.execute(delete(GoogleCalendarOAuthRequest).where(or_(
        GoogleCalendarOAuthRequest.professional_id == professional.id,
        GoogleCalendarOAuthRequest.expires_at <= now,
    )))
    db.add(GoogleCalendarOAuthRequest(
        id=request_id, professional_id=professional.id,
        token_version=professional.token_version, expires_at=now + timedelta(minutes=10),
    ))
    await db.commit()
    return url


async def consume_authorization(db: AsyncSession, state: str, browser_nonce: str) -> tuple[UUID, int]:
    professional_id, request_id, token_version = decode_oauth_state(state, browser_nonce)
    consumed = await db.scalar(delete(GoogleCalendarOAuthRequest).where(
        GoogleCalendarOAuthRequest.id == request_id,
        GoogleCalendarOAuthRequest.professional_id == professional_id,
        GoogleCalendarOAuthRequest.token_version == token_version,
        GoogleCalendarOAuthRequest.expires_at > datetime.now(UTC),
    ).returning(GoogleCalendarOAuthRequest.id))
    if consumed is None:
        raise GoogleCalendarError("A autorização do Google expirou ou já foi utilizada.")
    # Consome antes do I/O: um callback concorrente não pode trocar outro código.
    await db.commit()
    professional = await db.scalar(select(Professional.id).where(
        Professional.id == professional_id,
        Professional.token_version == token_version,
        Professional.is_disabled.is_(False),
        Professional.email_verified_at.is_not(None),
    ))
    if professional is None:
        raise GoogleCalendarError("A sessão iniciadora não é mais válida. Conecte novamente.")
    return professional_id, token_version


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


async def save_connection(db: AsyncSession, professional_id: UUID, token_payload: dict, *, token_version: int) -> None:
    professional = await db.scalar(select(Professional.id).where(
        Professional.id == professional_id,
        Professional.token_version == token_version,
        Professional.is_disabled.is_(False),
        Professional.email_verified_at.is_not(None),
    ).with_for_update())
    if professional is None:
        raise GoogleCalendarError("A sessão iniciadora não é mais válida. Conecte novamente.")
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
    # Serializa também a primeira inserção, antes de existir uma linha na fila.
    await db.execute(select(Appointment.id).where(Appointment.id == appointment.id).with_for_update())
    record = (
        await db.execute(
            select(GoogleCalendarSyncRecord).where(
                GoogleCalendarSyncRecord.appointment_id == appointment.id
            ).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    resolved_operation = operation or ("delete" if appointment.status == "cancelado" else "upsert")
    if record is None:
        record = GoogleCalendarSyncRecord(
            professional_id=appointment.professional_id,
            appointment_id=appointment.id,
        )
        db.add(record)
    else:
        record.sync_version += 1
    record.operation = resolved_operation
    record.event_snapshot = appointment_snapshot(appointment, patient_name)
    if record.processing_token is None:
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


async def _dispatch_once(record_id: UUID) -> bool:
    async with AsyncSessionLocal() as db:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=10)
        claim_token = uuid4()
        record = await db.scalar(update(GoogleCalendarSyncRecord).where(
            GoogleCalendarSyncRecord.id == record_id,
            or_(
                GoogleCalendarSyncRecord.status == "queued",
                (GoogleCalendarSyncRecord.status == "failed") & (GoogleCalendarSyncRecord.attempt_count < 5),
                (GoogleCalendarSyncRecord.status == "processing") & or_(
                    GoogleCalendarSyncRecord.processing_started_at < stale,
                    GoogleCalendarSyncRecord.processing_started_at.is_(None) & (GoogleCalendarSyncRecord.updated_at < stale),
                ),
            ),
        ).values(status="processing", processing_token=claim_token, processing_started_at=now,
                 attempt_count=GoogleCalendarSyncRecord.attempt_count + 1).returning(GoogleCalendarSyncRecord))
        if record is None:
            return False
        connection = (
            await db.execute(
                select(GoogleCalendarConnection).where(
                    GoogleCalendarConnection.professional_id == record.professional_id
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            return False
        claimed_version = record.sync_version
        operation = record.operation
        snapshot = record.event_snapshot or {}
        event_id = record.google_event_id
        await db.commit()
        message = None
        try:
            token = await _access_token(connection)
            calendar_path = quote(connection.calendar_id, safe="")
            if operation == "delete":
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
                create_event = not event_id
                if event_id:
                    response = await _google_request(
                        token,
                        "PUT",
                        f"/calendars/{calendar_path}/events/{quote(event_id, safe='')}",
                        json_body=body,
                    )
                    if response.status_code == 410:
                        event_id = None
                    create_event = response.status_code in {404, 410}
                if create_event:
                    # Persiste o ID antes do POST: timeout/retry reutiliza o mesmo ID.
                    event_id = event_id or "korus" + uuid4().hex
                    remembered = await db.execute(update(GoogleCalendarSyncRecord).where(
                        GoogleCalendarSyncRecord.id == record_id,
                        GoogleCalendarSyncRecord.processing_token == claim_token,
                    ).values(google_event_id=event_id))
                    if remembered.rowcount != 1:
                        return False
                    await db.commit()
                    body["id"] = event_id
                    response = await _google_request(
                        token,
                        "POST",
                        f"/calendars/{calendar_path}/events",
                        json_body=body,
                    )
                    if response.status_code == 409:
                        response = await _google_request(
                            token, "PUT", f"/calendars/{calendar_path}/events/{quote(event_id, safe='')}",
                            json_body=body,
                        )
                if response.status_code >= 400:
                    raise GoogleCalendarError("Não foi possível salvar o evento no Google Agenda.")
                event_id = str(response.json().get("id") or event_id or "") or None
        except Exception as exc:
            message = str(exc)[:500] if isinstance(exc, GoogleCalendarError) else "Falha temporária ao sincronizar com o Google Agenda."
            logger.warning("Google Calendar sync failed for record %s: %s", record.id, type(exc).__name__)
        record = await db.scalar(select(GoogleCalendarSyncRecord).where(
            GoogleCalendarSyncRecord.id == record_id,
            GoogleCalendarSyncRecord.processing_token == claim_token,
        ).with_for_update().execution_options(populate_existing=True))
        if record is None:
            return False
        changed = record.sync_version != claimed_version
        record.google_event_id = event_id
        record.status = "queued" if changed else "failed" if message else "synced"
        record.processing_token = None
        record.processing_started_at = None
        record.last_error = None if changed else message
        record.processed_at = None if changed or message else datetime.now(UTC)
        connection_values = {"last_error": None if changed else message}
        if record.processed_at:
            connection_values["last_sync_at"] = record.processed_at
        await db.execute(update(GoogleCalendarConnection).where(
            GoogleCalendarConnection.id == connection.id,
        ).values(**connection_values))
        await db.commit()
        return changed


async def dispatch_sync_record(record_id: UUID) -> None:
    # ponytail: drena até 5 versões; edição contínua deixa a próxima para o cron.
    for _ in range(5):
        if not await _dispatch_once(record_id):
            return


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
                                & or_(
                                    GoogleCalendarSyncRecord.processing_started_at < stale_processing,
                                    GoogleCalendarSyncRecord.processing_started_at.is_(None)
                                    & (GoogleCalendarSyncRecord.updated_at < stale_processing),
                                )
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
