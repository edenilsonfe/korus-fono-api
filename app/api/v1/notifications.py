"""User-facing in-app notification inbox endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.services.notification_service import (
    NotificationNotVisibleError,
    NotificationService,
)
from app.services.notification_settings_service import NotificationSettingsService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/settings", response_model=InAppNotificationSettings)
async def get_notification_settings(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    settings = await NotificationSettingsService(db).get_or_create(professional.id)
    return InAppNotificationSettings(birthday_in_app_enabled=settings.birthday_in_app_enabled)


@router.patch("/settings", response_model=InAppNotificationSettings)
async def update_notification_settings(
    payload: InAppNotificationSettings,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    settings = await NotificationSettingsService(db).update(
        professional.id, **payload.model_dump(exclude_unset=True)
    )
    return InAppNotificationSettings(birthday_in_app_enabled=settings.birthday_in_app_enabled)


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
