"""NotificationService — broadcasts and personal birthday reminders.

Distinct from the outbound WhatsApp ``notification_*`` services. Backs the
unified inbox described in ``docs/notificacoes-in-app-design.md``.

The service is cross-dialect: it works on PostgreSQL (production) and SQLite
(tests). Audience matching is done in Python (announcement volume is small) to
avoid PostgreSQL-only ``string_to_array``; read upserts are manual
(SELECT + INSERT/UPDATE) to avoid dialect-specific ``ON CONFLICT`` helpers.
"""

import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_notification import AppNotification, AppNotificationRead
from app.models.professional import Professional
from app.schemas.app_notification import (
    AnnouncementCreate,
    AnnouncementStats,
    AnnouncementUpdate,
    NotificationFilter,
    NotificationItem,
    NotificationPage,
    UnreadCount,
)
from app.services.birthday_service import (
    birthday_reminder_enabled,
    ensure_birthday_reminder,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Status transitions (announcement state machine).
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"published", "scheduled", "draft"},
    "scheduled": {"published", "draft", "scheduled"},
    "published": {"archived", "draft", "scheduled", "published"},
    "archived": {"draft", "scheduled", "published", "archived"},
}


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------ #
    # Admin — announcements
    # ------------------------------------------------------------------ #

    async def create_announcement(
        self,
        *,
        author: Professional,
        payload: AnnouncementCreate,
    ) -> AppNotification:
        notification = AppNotification(
            kind="broadcast",
            type=payload.type,
            title=payload.title,
            body=payload.body,
            deep_link=payload.deep_link,
            severity=payload.severity,
            audience=payload.audience,
            status="draft",
            publish_at=payload.publish_at,
            expires_at=payload.expires_at,
            created_by=author.id,
        )
        self.db.add(notification)
        await self.db.flush()
        return notification

    async def list_announcements(
        self,
        *,
        status_filter: str | None = None,
    ) -> list[AppNotification]:
        stmt = (
            select(AppNotification)
            .where(AppNotification.kind == "broadcast")
            .order_by(AppNotification.created_at.desc())
        )
        if status_filter:
            stmt = stmt.where(AppNotification.status == status_filter)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_announcement(self, notification_id: uuid.UUID) -> AppNotification | None:
        stmt = select(AppNotification).where(
            AppNotification.id == notification_id,
            AppNotification.kind == "broadcast",
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_announcement(
        self,
        *,
        notification_id: uuid.UUID,
        payload: AnnouncementUpdate,
    ) -> AppNotification:
        notification = await self.get_announcement(notification_id)
        if notification is None:
            raise AnnouncementNotFoundError(notification_id)

        data = payload.model_dump(exclude_unset=True)

        # Status transition validation (when status is being changed).
        new_status = data.get("status")
        if new_status is not None and new_status != notification.status:
            current = notification.status or "draft"
            allowed = _VALID_TRANSITIONS.get(current, set())
            if new_status not in allowed:
                raise InvalidStatusTransitionError(current, new_status)
            # Agendar: se status=scheduled, exigir publish_at futuro.
            if new_status == "scheduled":
                publish_at = data.get("publish_at", notification.publish_at)
                if publish_at is None or publish_at <= datetime.now(timezone.utc):
                    raise InvalidStatusTransitionError(
                        current, "scheduled (publish_at deve ser futuro)"
                    )
            notification.status = new_status

        # Scalar field updates (status already handled).
        for field in (
            "type",
            "title",
            "body",
            "severity",
            "deep_link",
            "audience",
            "publish_at",
            "expires_at",
        ):
            if field in data:
                setattr(notification, field, data[field])

        await self.db.flush()
        return notification

    async def delete_announcement(self, notification_id: uuid.UUID) -> None:
        notification = await self.get_announcement(notification_id)
        if notification is None:
            raise AnnouncementNotFoundError(notification_id)
        await self.db.delete(notification)
        await self.db.flush()

    async def announcement_stats(
        self, notification_id: uuid.UUID
    ) -> AnnouncementStats:
        notification = await self.get_announcement(notification_id)
        if notification is None:
            raise AnnouncementNotFoundError(notification_id)

        audience_size = await self._audience_size(notification.audience)

        reads_stmt = select(
            func.count().filter(AppNotificationRead.seen_at.is_not(None)),
            func.count().filter(AppNotificationRead.read_at.is_not(None)),
        ).where(AppNotificationRead.notification_id == notification_id)
        seen_count, read_count = (await self.db.execute(reads_stmt)).one()

        seen_rate = seen_count / audience_size if audience_size else 0.0
        read_rate = read_count / audience_size if audience_size else 0.0

        return AnnouncementStats(
            audience_size=audience_size,
            seen_count=seen_count,
            read_count=read_count,
            click_count=read_count,  # click == read_at in v1
            seen_rate=round(seen_rate, 4),
            read_rate=round(read_rate, 4),
            click_rate=round(read_rate, 4),
        )

    async def _audience_size(self, audience: str | None) -> int:
        stmt = select(func.count()).select_from(Professional)
        if audience and audience != "all":
            specialties = [s.strip() for s in audience.split(",") if s.strip()]
            if specialties:
                stmt = stmt.where(Professional.specialty_key.in_(specialties))
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    # ------------------------------------------------------------------ #
    # Usuário — inbox
    # ------------------------------------------------------------------ #

    def _audience_matches(self, audience: str | None, specialty_key: str) -> bool:
        if audience is None or audience == "all":
            return True
        specialties = {s.strip() for s in audience.split(",") if s.strip()}
        return specialty_key in specialties

    def _is_vigent(self, notification: AppNotification, now: datetime) -> bool:
        if notification.status not in ("published", "scheduled"):
            return False
        publish_at = _as_aware(notification.publish_at)
        expires_at = _as_aware(notification.expires_at)
        now = _as_aware(now)
        if publish_at is not None and publish_at > now:
            return False
        if expires_at is not None and expires_at <= now:
            return False
        return True

    def _is_visible_to(
        self, notification: AppNotification, professional: Professional, now: datetime
    ) -> bool:
        if notification.kind != "broadcast":
            return (
                notification.recipient_professional_id == professional.id
                and self._is_vigent(notification, now)
            )
        return self._is_vigent(notification, now) and self._audience_matches(
            notification.audience, professional.specialty_key
        )

    async def _fetch_visible_with_reads(
        self, professional: Professional, now: datetime
    ) -> list[tuple[AppNotification, datetime | None, datetime | None]]:
        ar = AppNotificationRead
        n = AppNotification
        birthdays_enabled = await ensure_birthday_reminder(self.db, professional.id, now)

        stmt = (
            select(
                n,
                ar.seen_at.label("seen_at"),
                ar.read_at.label("read_at"),
            )
            .outerjoin(
                ar,
                and_(
                    ar.notification_id == n.id,
                    ar.professional_id == professional.id,
                ),
            )
            .where(
                or_(
                    n.kind == "broadcast",
                    and_(
                        n.kind == "personal",
                        n.recipient_professional_id == professional.id,
                    ),
                )
            )
            .order_by(func.coalesce(n.publish_at, n.created_at).desc(), n.id.desc())
        )
        result = await self.db.execute(stmt)
        rows = []
        for row in result.all():
            notification: AppNotification = row[0]
            if notification.type == "birthday" and not birthdays_enabled:
                continue
            if self._is_visible_to(notification, professional, now):
                rows.append((notification, row[1], row[2]))
        return rows

    async def list_for_professional(
        self,
        *,
        professional: Professional,
        filter: NotificationFilter = "all",
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> NotificationPage:
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        now = datetime.now(timezone.utc)

        rows = await self._fetch_visible_with_reads(professional, now)

        if filter == "broadcast":
            rows = [row for row in rows if row[0].kind == "broadcast"]

        # Cursor pagination over (sort_ts DESC, id DESC).
        if cursor:
            sort_ts, last_id = _decode_cursor(cursor)
            if sort_ts is not None and last_id is not None:
                def _after(row):
                    notification = row[0]
                    s = _as_aware(notification.publish_at or notification.created_at)
                    cursor_ts = _as_aware(sort_ts)
                    return (s < cursor_ts) or (s == cursor_ts and notification.id < last_id)
                rows = [r for r in rows if _after(r)]

        next_cursor = None
        if len(rows) > limit:
            page = rows[:limit]
            last_n = page[-1][0]
            sort_ts = last_n.publish_at or last_n.created_at
            next_cursor = _encode_cursor(sort_ts, last_n.id)
        else:
            page = rows

        items = [_row_to_item(r) for r in page]
        return NotificationPage(items=items, next_cursor=next_cursor)

    async def counts_for_professional(
        self, professional: Professional
    ) -> UnreadCount:
        now = datetime.now(timezone.utc)
        rows = await self._fetch_visible_with_reads(professional, now)
        badge = sum(1 for r in rows if r[1] is None)
        unread = sum(1 for r in rows if r[2] is None)
        return UnreadCount(badge=badge, unread=unread)

    async def mark_seen(self, professional: Professional) -> UnreadCount:
        """Mark all vigent broadcasts as seen for this professional (upsert)."""
        now = datetime.now(timezone.utc)
        rows = await self._fetch_visible_with_reads(professional, now)
        ids_to_see = [r[0].id for r in rows if r[1] is None]
        if ids_to_see:
            await self._upsert_reads(
                professional.id, ids_to_see, set_seen=True, set_read=False, now=now
            )
            await self.db.flush()
        return await self.counts_for_professional(professional)

    async def mark_read(
        self, professional: Professional, notification_id: uuid.UUID
    ) -> NotificationItem:
        now = datetime.now(timezone.utc)
        stmt = select(AppNotification).where(AppNotification.id == notification_id)
        result = await self.db.execute(stmt)
        notification: AppNotification | None = result.scalar_one_or_none()
        if notification is None or not self._is_visible_to(
            notification, professional, now
        ):
            raise NotificationNotVisibleError(notification_id)

        if notification.type == "birthday" and not await birthday_reminder_enabled(
            self.db, professional.id
        ):
            raise NotificationNotVisibleError(notification_id)

        await self._upsert_reads(
            professional.id, [notification_id], set_seen=True, set_read=True, now=now
        )
        await self.db.flush()

        return NotificationItem(
            id=str(notification.id),
            kind=notification.kind,
            type=notification.type,
            title=notification.title,
            body=notification.body,
            deep_link=notification.deep_link,
            severity=notification.severity,
            seen=True,
            read=True,
            created_at=notification.created_at,
            sort_ts=notification.publish_at or notification.created_at,
        )

    async def mark_all_read(self, professional: Professional) -> UnreadCount:
        now = datetime.now(timezone.utc)
        rows = await self._fetch_visible_with_reads(professional, now)
        ids_to_read = [r[0].id for r in rows if r[2] is None]
        if ids_to_read:
            await self._upsert_reads(
                professional.id, ids_to_read, set_seen=True, set_read=True, now=now
            )
            await self.db.flush()
        return await self.counts_for_professional(professional)

    async def _upsert_reads(
        self,
        professional_id: uuid.UUID,
        notification_ids: list[uuid.UUID],
        *,
        set_seen: bool,
        set_read: bool,
        now: datetime,
    ) -> None:
        if not notification_ids:
            return

        ar = AppNotificationRead

        # Fetch existing read rows for this (prof, notif_ids) pair.
        existing_stmt = select(ar).where(
            ar.professional_id == professional_id,
            ar.notification_id.in_(notification_ids),
        )
        existing_result = await self.db.execute(existing_stmt)
        existing_by_notif = {
            row.notification_id: row for row in existing_result.scalars().all()
        }

        for nid in notification_ids:
            existing = existing_by_notif.get(nid)
            if existing is None:
                self.db.add(
                    ar(
                        notification_id=nid,
                        professional_id=professional_id,
                        seen_at=now if set_seen else None,
                        read_at=now if set_read else None,
                    )
                )
            else:
                if set_seen and existing.seen_at is None:
                    existing.seen_at = now
                if set_read and existing.read_at is None:
                    existing.read_at = now
        await self.db.flush()


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC.

    SQLite returns naive datetimes even for ``DateTime(timezone=True)`` columns;
    PostgreSQL returns aware ones. Comparisons between aware and naive
    datetimes raise ``TypeError``, so we normalize everything to aware UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _encode_cursor(sort_ts: datetime | None, last_id: uuid.UUID) -> str:
    ts_iso = sort_ts.isoformat() if sort_ts else ""
    raw = f"{ts_iso}|{last_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID | None]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
        ts_part, id_part = raw.split("|", 1)
        sort_ts = datetime.fromisoformat(ts_part) if ts_part else None
        last_id = uuid.UUID(id_part)
        return sort_ts, last_id
    except (ValueError, TypeError):
        return None, None


def _row_to_item(row) -> NotificationItem:
    notification: AppNotification = row[0]
    seen_at = row[1]
    read_at = row[2]
    return NotificationItem(
        id=str(notification.id),
        kind=notification.kind,
        type=notification.type,
        title=notification.title,
        body=notification.body,
        deep_link=notification.deep_link,
        severity=notification.severity,
        seen=seen_at is not None,
        read=read_at is not None,
        created_at=notification.created_at,
        sort_ts=notification.publish_at or notification.created_at,
    )


# ---------------------------------------------------------------------- #
# Errors
# ---------------------------------------------------------------------- #


class AnnouncementNotFoundError(Exception):
    def __init__(self, notification_id):
        super().__init__(f"Announcement {notification_id} not found")
        self.notification_id = notification_id


class NotificationNotVisibleError(Exception):
    def __init__(self, notification_id):
        super().__init__(f"Notification {notification_id} not visible")
        self.notification_id = notification_id


class InvalidStatusTransitionError(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"Invalid status transition: {current} -> {target}")
        self.current = current
        self.target = target
