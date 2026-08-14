"""Read-only queries for WhatsApp notification message logs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_message_log import NotificationMessageLog
from app.models.patient import Patient
from app.services.evolution_whatsapp_service import mask_phone

EVENT_LABELS: dict[str, str] = {
    "appointment_reminder_24h": "Lembrete 24h",
    "appointment_confirmation": "Confirmação de sessão",
    "appointment_cancelled": "Atendimento cancelado",
    "appointment_rescheduled": "Reagendamento",
    "billing_reminder": "Lembrete de pagamento",
    "billing_overdue": "Pagamento em atraso",
}

STATUS_LABELS: dict[str, str] = {
    "queued": "Na fila",
    "processing": "Processando",
    "sent": "Enviada",
    "delivered": "Entregue",
    "read": "Lida",
    "failed": "Falhou",
    "skipped": "Ignorada",
    "superseded": "Substituída",
}


def _event_label(notification_type: str) -> str:
    return EVENT_LABELS.get(notification_type, notification_type.replace("_", " ").title())


def _status_label(status: str, *, no_phone: bool) -> str:
    if no_phone:
        return "Sem telefone"
    return STATUS_LABELS.get(status, status)


def _is_no_phone(log: NotificationMessageLog) -> bool:
    if log.to_phone:
        return False
    if log.error_code in {"no_phone", "missing_phone", "invalid_phone"}:
        return True
    if log.last_error and "telefone" in log.last_error.lower():
        return True
    return log.status == "failed" and not log.to_phone


def _delivery_seconds(log: NotificationMessageLog) -> int | None:
    start = log.sent_at or log.created_at
    end = log.delivered_at or log.read_at
    if not start or not end:
        return None
    return max(int((end - start).total_seconds()), 0)


class WhatsAppMessageLogService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _base_filters(self, professional_id: UUID, *, days: int = 30):
        since = datetime.now(UTC) - timedelta(days=days)
        return and_(
            NotificationMessageLog.professional_id == professional_id,
            NotificationMessageLog.channel == "whatsapp",
            NotificationMessageLog.is_test.is_(False),
            NotificationMessageLog.created_at >= since,
        )

    async def get_stats(self, professional_id: UUID, *, days: int = 30) -> dict[str, int]:
        since = datetime.now(UTC) - timedelta(days=days)
        result = await self.db.execute(
            select(NotificationMessageLog).where(
                NotificationMessageLog.professional_id == professional_id,
                NotificationMessageLog.channel == "whatsapp",
                NotificationMessageLog.is_test.is_(False),
                NotificationMessageLog.created_at >= since,
            )
        )
        logs = result.scalars().all()

        sent = delivered = queued = skipped = failed = no_phone = 0
        for log in logs:
            if _is_no_phone(log):
                no_phone += 1
                continue
            if log.status in {"sent", "delivered", "read"}:
                sent += 1
            if log.status in {"queued", "processing"}:
                queued += 1
            if log.status in {"skipped", "superseded"}:
                skipped += 1
            if log.status in {"delivered", "read"}:
                delivered += 1
            if log.status == "failed":
                failed += 1

        return {
            "period_days": days,
            "sent": sent,
            "delivered": delivered,
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
            "no_phone": no_phone,
            "total": len(logs),
        }

    async def list_logs(
        self,
        professional_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        event_type: str | None = None,
        status: str | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        filters = [self._base_filters(professional_id, days=days)]

        if event_type and event_type != "all":
            filters.append(NotificationMessageLog.notification_type == event_type)

        if status and status != "all":
            if status == "no_phone":
                filters.append(
                    or_(
                        NotificationMessageLog.to_phone.is_(None),
                        NotificationMessageLog.error_code.in_(
                            ["no_phone", "missing_phone", "invalid_phone"]
                        ),
                    )
                )
            else:
                if status == "queued":
                    filters.append(
                        NotificationMessageLog.status.in_(["queued", "processing"])
                    )
                elif status == "skipped":
                    filters.append(
                        NotificationMessageLog.status.in_(["skipped", "superseded"])
                    )
                else:
                    filters.append(NotificationMessageLog.status == status)

        where_clause = and_(*filters)

        total_result = await self.db.execute(
            select(func.count(NotificationMessageLog.id)).where(where_clause)
        )
        total = int(total_result.scalar_one() or 0)
        total_pages = max(ceil(total / page_size), 1) if total else 1

        result = await self.db.execute(
            select(NotificationMessageLog, Patient.name)
            .outerjoin(Patient, Patient.id == NotificationMessageLog.patient_id)
            .where(where_clause)
            .order_by(NotificationMessageLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        items = []
        for log, patient_name in result.all():
            no_phone = _is_no_phone(log)
            payload = log.payload if isinstance(log.payload, dict) else {}
            dispatch_decision = payload.get("dispatch_decision")
            items.append(
                {
                    "id": str(log.id),
                    "appointment_id": (
                        str(log.appointment_id) if log.appointment_id else None
                    ),
                    "created_at": log.created_at,
                    "notification_type": log.notification_type,
                    "event_label": _event_label(log.notification_type),
                    "recipient_name": patient_name,
                    "recipient_phone": mask_phone(log.to_phone) if log.to_phone else log.to_phone,
                    "template_name": None,
                    "status": log.status,
                    "status_label": _status_label(log.status, no_phone=no_phone),
                    "delivery_seconds": _delivery_seconds(log),
                    "scheduled_date": log.scheduled_date,
                    "scheduled_time": log.scheduled_time,
                    "attempt_count": log.attempt_count,
                    "provider_message_id": log.provider_message_id,
                    "skip_reason": payload.get("skip_reason"),
                    "dispatch_decision": (
                        dispatch_decision
                        if isinstance(dispatch_decision, dict)
                        else None
                    ),
                    "last_error": log.last_error,
                    "is_test": log.is_test,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
