"""Admin endpoints for broadcast announcements (platform staff only)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_PRODUCT_READ, PERMISSION_PRODUCT_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.app_notification import (
    Announcement,
    AnnouncementCreate,
    AnnouncementStats,
    AnnouncementUpdate,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.notification_service import (
    AnnouncementNotFoundError,
    InvalidStatusTransitionError,
    NotificationService,
)

router = APIRouter(prefix="/announcements", tags=["admin-announcements"])


def _to_announcement(n) -> Announcement:
    return Announcement(
        id=str(n.id),
        type=n.type,
        title=n.title,
        body=n.body,
        severity=n.severity,
        deep_link=n.deep_link,
        audience=n.audience,
        status=n.status,
        publish_at=n.publish_at,
        expires_at=n.expires_at,
        created_by=str(n.created_by) if n.created_by else None,
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


@router.post("", response_model=Announcement, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=Announcement, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    payload: AnnouncementCreate,
    author: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    notification = await service.create_announcement(author=author, payload=payload)
    await AdminAuditService(db).log(
        actor=author,
        action="create_announcement",
        payload={"notification_id": str(notification.id), "title": notification.title},
    )
    await db.commit()
    await db.refresh(notification)
    return _to_announcement(notification)


@router.get("", response_model=list[Announcement])
@router.get("/", response_model=list[Announcement])
async def list_announcements(
    status_filter: str | None = Query(None, alias="status"),
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    items = await service.list_announcements(status_filter=status_filter)
    return [_to_announcement(n) for n in items]


@router.get("/{notification_id}", response_model=Announcement)
async def get_announcement(
    notification_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    notification = await service.get_announcement(notification_id)
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Anúncio não encontrado",
        )
    return _to_announcement(notification)


@router.patch("/{notification_id}", response_model=Announcement)
async def update_announcement(
    notification_id: UUID,
    payload: AnnouncementUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    try:
        notification = await service.update_announcement(
            notification_id=notification_id, payload=payload
        )
    except AnnouncementNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Anúncio não encontrado",
        ) from exc
    except InvalidStatusTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Transição de status inválida: {exc.current} -> {exc.target}",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="update_announcement",
        payload={"notification_id": str(notification.id), "title": notification.title, "after": notification.status},
    )
    await db.commit()
    await db.refresh(notification)
    return _to_announcement(notification)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(
    notification_id: UUID,
    actor: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    try:
        await service.delete_announcement(notification_id)
    except AnnouncementNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Anúncio não encontrado",
        ) from exc
    await AdminAuditService(db).log(
        actor=actor,
        action="delete_announcement",
        payload={"notification_id": str(notification_id)},
    )
    await db.commit()


@router.get("/{notification_id}/stats", response_model=AnnouncementStats)
async def announcement_stats(
    notification_id: UUID,
    _: Professional = Depends(require_admin_permission(PERMISSION_PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationService(db)
    try:
        return await service.announcement_stats(notification_id)
    except AnnouncementNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Anúncio não encontrado",
        ) from exc
