from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_audit_log import AdminAuditLog
from app.models.professional import Professional
from app.schemas.admin_operations import AdminAuditEventItem, AdminAuditEventsPage

_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "card",
    "cvv",
    "authorization",
    "credential",
)


def _redact_payload(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(fragment in key.lower() for fragment in _SENSITIVE_KEY_FRAGMENTS)
                else _redact_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


class AdminAuditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(
        self,
        *,
        action: str,
        actor_id: UUID | None = None,
        actor: Professional | None = None,
        target_professional_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AdminAuditLog:
        if actor is None and actor_id is not None:
            actor = await self.db.get(Professional, actor_id)
        resolved_actor_id = actor.id if actor is not None else actor_id
        entry = AdminAuditLog(
            actor_id=resolved_actor_id,
            actor_name=actor.name if actor is not None else None,
            actor_email=actor.email if actor is not None else None,
            target_professional_id=target_professional_id,
            action=action,
            payload=payload,
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def list_events(
        self,
        *,
        target_professional_id: UUID | None = None,
        actor_id: UUID | None = None,
        action: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> AdminAuditEventsPage:
        page = max(page, 1)
        limit = min(max(limit, 1), 100)
        filters = []
        if target_professional_id is not None:
            filters.append(AdminAuditLog.target_professional_id == target_professional_id)
        if actor_id is not None:
            filters.append(AdminAuditLog.actor_id == actor_id)
        if action:
            filters.append(AdminAuditLog.action == action)

        count_stmt = select(func.count()).select_from(AdminAuditLog)
        list_stmt = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)

        total = int((await self.db.execute(count_stmt)).scalar_one())
        rows = (
            await self.db.execute(list_stmt.offset((page - 1) * limit).limit(limit))
        ).scalars().all()
        return AdminAuditEventsPage(
            items=[
                AdminAuditEventItem(
                    id=str(row.id),
                    actor_id=str(row.actor_id) if row.actor_id else None,
                    actor_name=row.actor_name,
                    actor_email=row.actor_email,
                    target_professional_id=(
                        str(row.target_professional_id) if row.target_professional_id else None
                    ),
                    action=row.action,
                    reason=(row.payload or {}).get("reason"),
                    payload=_redact_payload(row.payload),
                    created_at=row.created_at,
                )
                for row in rows
            ],
            total=total,
            page=page,
            limit=limit,
        )
