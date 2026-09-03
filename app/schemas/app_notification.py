"""Schemas for the in-app notification inbox (broadcast announcements).

Distinct from ``notification_settings`` (outbound clinic -> patient WhatsApp).
Backs the unified inbox described in ``docs/notificacoes-in-app-design.md``.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from app.schemas.common import CamelModel

NotificationKind = Literal["broadcast", "personal"]
NotificationType = Literal["feature", "tutorial", "notice"]
NotificationSeverity = Literal["info", "success", "warning", "critical"]
AnnouncementStatus = Literal["draft", "scheduled", "published", "archived"]
NotificationFilter = Literal["all", "broadcast"]


class InAppNotificationSettings(CamelModel):
    birthday_in_app_enabled: bool = False


class NotificationItem(CamelModel):
    """A single inbox row with per-professional seen/read state derived from reads."""

    id: str
    kind: NotificationKind
    type: str
    title: str
    body: str
    deep_link: Optional[str] = None
    severity: str
    seen: bool
    read: bool
    created_at: datetime
    sort_ts: datetime


class NotificationPage(CamelModel):
    items: list[NotificationItem]
    next_cursor: Optional[str] = None


class UnreadCount(CamelModel):
    badge: int  # unseen
    unread: int  # unread


# --- Admin announcements (broadcast) ---


class AnnouncementCreate(CamelModel):
    type: NotificationType
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    severity: NotificationSeverity = "info"
    deep_link: Optional[str] = Field(default=None, max_length=500)
    audience: str = Field(default="all", max_length=500)
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class AnnouncementUpdate(CamelModel):
    type: Optional[NotificationType] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    body: Optional[str] = Field(default=None, min_length=1)
    severity: Optional[NotificationSeverity] = None
    deep_link: Optional[str] = Field(default=None, max_length=500)
    audience: Optional[str] = Field(default=None, max_length=500)
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    status: Optional[AnnouncementStatus] = None


class Announcement(CamelModel):
    id: str
    type: str
    title: str
    body: str
    severity: str
    deep_link: Optional[str] = None
    audience: Optional[str] = None
    status: Optional[str] = None
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class AnnouncementStats(CamelModel):
    audience_size: int
    seen_count: int
    read_count: int
    click_count: int
    seen_rate: float
    read_rate: float
    click_rate: float
