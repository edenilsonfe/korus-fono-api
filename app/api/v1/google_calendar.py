from urllib.parse import urlencode
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.auth_cookies import GOOGLE_OAUTH_COOKIE, GOOGLE_OAUTH_COOKIE_PATH
from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.google_calendar import GoogleCalendarConnection, GoogleCalendarSyncRecord
from app.models.professional import Professional
from app.schemas.google_calendar import (
    GoogleCalendarAuthorizationResponse,
    GoogleCalendarSettingsUpdate,
    GoogleCalendarStatusResponse,
    GoogleCalendarSyncResponse,
)
from app.services.google_calendar_service import (
    GoogleCalendarError,
    begin_authorization,
    consume_authorization,
    dispatch_sync_records,
    exchange_authorization_code,
    queue_future_appointments,
    revoke_connection,
    save_connection,
    status_counts,
)

router = APIRouter(prefix="/google-calendar", tags=["google-calendar"])


def _frontend_redirect(result: str) -> RedirectResponse:
    base = get_settings().frontend_url.rstrip("/")
    response = RedirectResponse(f"{base}/configuracoes?{urlencode({'googleCalendar': result})}")
    response.delete_cookie(GOOGLE_OAUTH_COOKIE, path=GOOGLE_OAUTH_COOKIE_PATH,
                           secure=not get_settings().debug, httponly=True, samesite="lax")
    return response


@router.get("/status", response_model=GoogleCalendarStatusResponse)
async def get_status(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.professional_id == professional.id
            )
        )
    ).scalar_one_or_none()
    pending, failed = await status_counts(db, professional.id)
    return GoogleCalendarStatusResponse(
        configured=get_settings().google_calendar_configured,
        connected=connection is not None,
        include_patient_name=connection.include_patient_name if connection else False,
        connected_at=connection.connected_at if connection else None,
        last_sync_at=connection.last_sync_at if connection else None,
        last_error=connection.last_error if connection else None,
        pending_events=pending,
        failed_events=failed,
    )


@router.post("/oauth/authorize", response_model=GoogleCalendarAuthorizationResponse)
async def authorize(
    response: Response,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    try:
        browser_nonce = secrets.token_urlsafe(32)
        url = await begin_authorization(db, professional, browser_nonce)
        response.set_cookie(
            GOOGLE_OAUTH_COOKIE, browser_nonce, max_age=600,
            path=GOOGLE_OAUTH_COOKIE_PATH, secure=not get_settings().debug,
            httponly=True, samesite="lax",
        )
        return GoogleCalendarAuthorizationResponse(authorization_url=url)
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state_token: str | None = Query(default=None, alias="state"),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    if error or not code or not state_token:
        return _frontend_redirect("error")
    try:
        professional_id, token_version = await consume_authorization(
            db, state_token, request.cookies.get(GOOGLE_OAUTH_COOKIE, "")
        )
        payload = await exchange_authorization_code(code)
        await save_connection(db, professional_id, payload, token_version=token_version)
    except GoogleCalendarError:
        return _frontend_redirect("error")
    return _frontend_redirect("connected")


@router.patch("/settings", response_model=GoogleCalendarStatusResponse)
async def update_settings(
    body: GoogleCalendarSettingsUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.professional_id == professional.id
            )
        )
    ).scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=409, detail="Conecte o Google Agenda primeiro.")
    connection.include_patient_name = body.include_patient_name
    connection.last_error = None
    await db.commit()
    pending, failed = await status_counts(db, professional.id)
    return GoogleCalendarStatusResponse(
        configured=True,
        connected=True,
        include_patient_name=connection.include_patient_name,
        connected_at=connection.connected_at,
        last_sync_at=connection.last_sync_at,
        pending_events=pending,
        failed_events=failed,
    )


@router.post("/sync", response_model=GoogleCalendarSyncResponse, status_code=202)
async def sync_now(
    background_tasks: BackgroundTasks,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    connection = (
        await db.execute(
            select(GoogleCalendarConnection.id).where(
                GoogleCalendarConnection.professional_id == professional.id
            )
        )
    ).scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=409, detail="Conecte o Google Agenda primeiro.")
    records = await queue_future_appointments(db, professional.id)
    background_tasks.add_task(dispatch_sync_records, [record.id for record in records])
    return GoogleCalendarSyncResponse(
        queued_count=len(records),
        message="Sincronização iniciada.",
    )


@router.delete("/connection", status_code=204)
async def disconnect(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.professional_id == professional.id
            )
        )
    ).scalar_one_or_none()
    if connection:
        await revoke_connection(connection)
        await db.execute(
            delete(GoogleCalendarSyncRecord).where(
                GoogleCalendarSyncRecord.professional_id == professional.id
            )
        )
        await db.delete(connection)
        await db.commit()
