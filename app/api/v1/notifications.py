"""User-facing in-app notification inbox endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.app_notification import (
    InAppNotificationSettings,
    NotificationFilter,
    NotificationItem,
    NotificationPage,
    UnreadCount,
)
from app.schemas.common import MessageResponse
from app.services.notification_service import (
    NotificationNotVisibleError,
    NotificationService,
)
from app.services.notification_settings_service import NotificationSettingsService
from app.services.weekly_summary_email_service import (
    InvalidWeeklySummaryUnsubscribeToken,
    unsubscribe_weekly_summary_email,
    validate_weekly_summary_unsubscribe_token,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/settings", response_model=InAppNotificationSettings)
async def get_notification_settings(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    settings = await NotificationSettingsService(db).get_or_create(professional.id)
    return InAppNotificationSettings(
        birthday_in_app_enabled=settings.birthday_in_app_enabled,
        weekly_summary_email_enabled=settings.weekly_summary_email_enabled,
    )


@router.patch("/settings", response_model=InAppNotificationSettings)
async def update_notification_settings(
    payload: InAppNotificationSettings,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    settings = await NotificationSettingsService(db).update(
        professional.id, **payload.model_dump(exclude_unset=True)
    )
    return InAppNotificationSettings(
        birthday_in_app_enabled=settings.birthday_in_app_enabled,
        weekly_summary_email_enabled=settings.weekly_summary_email_enabled,
    )


@router.post("/weekly-summary/unsubscribe", response_model=MessageResponse)
async def unsubscribe_weekly_summary(
    token: str = Query(min_length=20, max_length=200),
    db: AsyncSession = Depends(get_db),
):
    try:
        await unsubscribe_weekly_summary_email(db, token)
    except InvalidWeeklySummaryUnsubscribeToken as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link de descadastro inválido.",
        ) from exc
    return MessageResponse(message="Resumo semanal desativado.")


@router.get("/weekly-summary/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_weekly_summary_landing(
    token: str = Query(min_length=20, max_length=200),
):
    try:
        validate_weekly_summary_unsubscribe_token(token)
    except InvalidWeeklySummaryUnsubscribeToken as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link de descadastro inválido.",
        ) from exc
    return HTMLResponse(
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Desativar resumo semanal</title></head>"
        "<body><main><h1>Desativar resumo semanal</h1>"
        "<p>Confirme para não receber novos resumos semanais do KorusFono.</p>"
        "<form method='post'><button type='submit'>Confirmar descadastro</button></form>"
        "</main></body></html>"
    )


@router.get("", response_model=NotificationPage)
@router.get("/", response_model=NotificationPage)
async def list_notifications(
    filter: NotificationFilter = Query("all"),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    return await service.list_for_professional(
        professional=professional, filter=filter, cursor=cursor, limit=limit
    )


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    return await service.counts_for_professional(professional)


@router.post("/seen", response_model=UnreadCount)
async def mark_seen(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Mark currently-visible notifications as seen (badge -> 0)."""
    service = NotificationService(db)
    return await service.mark_seen(professional)


@router.post("/{notification_id}/read", response_model=NotificationItem)
async def mark_read(
    notification_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single visible notification as read (404 if not visible)."""
    service = NotificationService(db)
    try:
        return await service.mark_read(professional, notification_id)
    except NotificationNotVisibleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notificação não encontrada",
        ) from exc


@router.post("/read-all", response_model=UnreadCount)
async def mark_all_read(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Mark every currently-visible notification as read."""
    service = NotificationService(db)
    return await service.mark_all_read(professional)
