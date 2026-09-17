from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import time as system_time
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.appointment import Appointment
from app.models.finance import (
    FinancialPayment,
    PaymentAllocation,
    Receivable,
)
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.weekly_summary_email import WeeklySummaryEmailDelivery
from app.services.email.resend_client import send_email
from app.services.email.templates import weekly_summary_email
from app.services.financial_service import cash_flow

UNSUBSCRIBE_TOKEN_VERSION = 1
UNSUBSCRIBE_TOKEN_SIGNATURE_BYTES = 16
UNSUBSCRIBE_TOKEN_BYTES = 1 + 16 + UNSUBSCRIBE_TOKEN_SIGNATURE_BYTES


class InvalidWeeklySummaryUnsubscribeToken(ValueError):
    pass


def _unsubscribe_signature(professional_id: UUID) -> bytes:
    message = b"\0".join(
        (
            b"weekly_summary_email_unsubscribe",
            bytes((UNSUBSCRIBE_TOKEN_VERSION,)),
            professional_id.bytes,
        )
    )
    return hmac.new(
        get_settings().jwt_secret.encode("utf-8"), message, hashlib.sha256
    ).digest()[:UNSUBSCRIBE_TOKEN_SIGNATURE_BYTES]


def create_weekly_summary_unsubscribe_token(professional_id: UUID) -> str:
    payload = (
        bytes((UNSUBSCRIBE_TOKEN_VERSION,))
        + professional_id.bytes
        + _unsubscribe_signature(professional_id)
    )
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_weekly_summary_unsubscribe_token(token: str) -> UUID:
    try:
        padding = b"=" * (-len(token) % 4)
        payload = base64.b64decode(
            token.encode("ascii") + padding,
            altchars=b"-_",
            validate=True,
        )
        if (
            len(payload) != UNSUBSCRIBE_TOKEN_BYTES
            or payload[0] != UNSUBSCRIBE_TOKEN_VERSION
        ):
            raise ValueError("invalid unsubscribe token")
        professional_id = UUID(bytes=payload[1:17])
        if not hmac.compare_digest(payload[17:], _unsubscribe_signature(professional_id)):
            raise ValueError("invalid unsubscribe signature")
        return professional_id
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise InvalidWeeklySummaryUnsubscribeToken from exc


def validate_weekly_summary_unsubscribe_token(token: str) -> None:
    _decode_weekly_summary_unsubscribe_token(token)


async def unsubscribe_weekly_summary_email(db: AsyncSession, token: str) -> None:
    professional_id = _decode_weekly_summary_unsubscribe_token(token)
    settings = await db.scalar(
        select(NotificationSettings)
        .where(NotificationSettings.professional_id == professional_id)
        .with_for_update()
    )
    if settings is None or not settings.weekly_summary_email_enabled:
        return
    settings.weekly_summary_email_enabled = False
    settings.weekly_summary_email_opted_out_at = utcnow()
    settings.weekly_summary_email_preference_source = "unsubscribe_link"
    await db.flush()


def verify_resend_webhook_signature(
    body: bytes,
    *,
    message_id: str,
    timestamp: str,
    signature_header: str,
) -> bool:
    secret = get_settings().resend_webhook_secret.strip()
    if not secret.startswith("whsec_") or not all(
        (message_id, timestamp, signature_header)
    ):
        return False
    try:
        timestamp_value = int(timestamp)
        if abs(int(system_time.time()) - timestamp_value) > 300:
            return False
        encoded_key = secret.removeprefix("whsec_").encode("ascii")
        key = base64.b64decode(
            encoded_key + b"=" * (-len(encoded_key) % 4), validate=True
        )
    except (binascii.Error, ValueError, UnicodeError):
        return False

    signed = f"{message_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()
    return any(
        version == "v1" and hmac.compare_digest(candidate, expected)
        for item in signature_header.split()
        if "," in item
        for version, candidate in (item.split(",", 1),)
    )


async def handle_resend_weekly_summary_event(
    db: AsyncSession, payload: dict
) -> None:
    event_type = payload.get("type")
    data = payload.get("data")
    if event_type not in {"email.bounced", "email.complained"} or not isinstance(
        data, dict
    ):
        return
    if event_type == "email.bounced":
        bounce = data.get("bounce")
        if not isinstance(bounce, dict) or bounce.get("type") != "Permanent":
            return
        delivery_status = "bounced"
        suppression_reason = "permanent_bounce"
    else:
        delivery_status = "complained"
        suppression_reason = "complaint"

    provider_message_id = data.get("email_id")
    if not isinstance(provider_message_id, str) or not provider_message_id:
        return
    delivery = await db.scalar(
        select(WeeklySummaryEmailDelivery)
        .where(WeeklySummaryEmailDelivery.provider_message_id == provider_message_id)
        .with_for_update()
    )
    if delivery is None:
        return
    delivery.status = delivery_status
    delivery.last_error = suppression_reason
    settings = await db.scalar(
        select(NotificationSettings)
        .where(NotificationSettings.professional_id == delivery.professional_id)
        .with_for_update()
    )
    if settings is not None:
        settings.weekly_summary_email_enabled = False
        settings.weekly_summary_email_opted_out_at = utcnow()
        settings.weekly_summary_email_preference_source = "resend_webhook"
        settings.weekly_summary_email_suppressed_at = utcnow()
        settings.weekly_summary_email_suppression_reason = suppression_reason
    await db.flush()


def _operational_week(now: datetime) -> tuple[date, date] | None:
    local = now.astimezone(ZoneInfo(get_settings().clinic_timezone))
    if local.weekday() == 5 and local.time() >= time(19):
        return local.date() - timedelta(days=5), local.date()
    if local.weekday() == 6 and local.time() < time(19):
        return local.date() - timedelta(days=6), local.date() - timedelta(days=1)
    return None


async def _appointment_snapshot(
    db: AsyncSession, professional_id: UUID, week_start: date, week_end: date
) -> dict:
    rows = await db.execute(
        select(Appointment.status, func.count(Appointment.id))
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Appointment.professional_id == professional_id,
            Patient.is_demo.is_(False),
            Appointment.date >= week_start,
            Appointment.date <= week_end,
            or_(Appointment.date < week_end, Appointment.time < time(19)),
            Appointment.status.in_(
                ("pendente", "confirmado", "concluido", "cancelado", "falta")
            ),
        )
        .group_by(Appointment.status)
    )
    counts = {status: int(count) for status, count in rows.all()}
    total = sum(counts.values())
    completed = counts.get("concluido", 0)
    no_show = counts.get("falta", 0)
    cancelled = counts.get("cancelado", 0)
    attendance_base = completed + no_show
    unfinished = counts.get("pendente", 0) + counts.get("confirmado", 0)
    return {
        "total": total,
        "completed": completed,
        "noShow": no_show,
        "cancelled": cancelled,
        "unfinished": unfinished,
        "attendanceRate": (
            round(completed * 100 / attendance_base, 1) if attendance_base else None
        ),
    }


async def _overdue_snapshot(
    db: AsyncSession, professional_id: UUID, as_of_date: date
) -> tuple[int, int]:
    paid = (
        select(
            PaymentAllocation.receivable_id.label("receivable_id"),
            func.sum(PaymentAllocation.amount_cents).label("paid_cents"),
        )
        .join(FinancialPayment, FinancialPayment.id == PaymentAllocation.payment_id)
        .where(FinancialPayment.status == "confirmed")
        .group_by(PaymentAllocation.receivable_id)
        .subquery()
    )
    balance = Receivable.total_cents - func.coalesce(paid.c.paid_cents, 0)
    row = (
        await db.execute(
            select(func.count(Receivable.id), func.coalesce(func.sum(balance), 0))
            .outerjoin(paid, paid.c.receivable_id == Receivable.id)
            .where(
                Receivable.professional_id == professional_id,
                Receivable.status != "canceled",
                Receivable.due_date < as_of_date,
                balance > 0,
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def build_weekly_summary_snapshot(
    db: AsyncSession,
    professional_id: UUID,
    week_start: date,
    week_end: date,
    as_of_date: date,
) -> dict:
    appointments = await _appointment_snapshot(db, professional_id, week_start, week_end)
    flow = await cash_flow(db, professional_id, week_start, week_end)
    overdue_count, overdue_balance = await _overdue_snapshot(
        db, professional_id, as_of_date
    )
    return {
        "appointments": appointments,
        "finance": {
            "receivedCents": flow.realized_income_cents,
            "paidExpensesCents": flow.realized_expense_cents,
            "balanceCents": flow.realized_balance_cents,
            "overdueCount": overdue_count,
            "overdueBalanceCents": overdue_balance,
        },
    }


def _has_summary_activity(snapshot: dict) -> bool:
    return bool(
        snapshot["appointments"]["total"]
        or snapshot["finance"]["receivedCents"]
        or snapshot["finance"]["paidExpensesCents"]
        or snapshot["finance"]["overdueCount"]
    )


def _retry_at(now: datetime, attempt_count: int) -> datetime | None:
    delays = (15, 60, 240, 720)
    if attempt_count > len(delays):
        return None
    return now + timedelta(minutes=delays[attempt_count - 1])


async def _eligible_professionals(
    db: AsyncSession, now: datetime, closing_at: datetime
) -> list[Professional]:
    result = await db.execute(
        select(Professional)
        .join(
            NotificationSettings,
            NotificationSettings.professional_id == Professional.id,
        )
        .where(
            NotificationSettings.weekly_summary_email_enabled.is_(True),
            NotificationSettings.weekly_summary_email_opted_in_at.is_not(None),
            NotificationSettings.weekly_summary_email_opted_in_at < closing_at,
            NotificationSettings.weekly_summary_email_suppressed_at.is_(None),
            Professional.is_staff.is_(False),
            Professional.is_disabled.is_(False),
            Professional.email_verified_at.is_not(None),
            Professional.signup_payment_required.is_(False),
            or_(
                Professional.subscription_status == "active",
                and_(
                    Professional.subscription_status == "trialing",
                    or_(
                        Professional.trial_ends_at.is_(None),
                        Professional.trial_ends_at >= now,
                    ),
                ),
            ),
        )
        .order_by(Professional.id)
    )
    return list(result.scalars().all())


async def run_weekly_summary_emails(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    current = now or datetime.now(UTC)
    week = _operational_week(current)
    if week is None:
        return 0
    week_start, week_end = week
    timezone = ZoneInfo(get_settings().clinic_timezone)
    closing_at = datetime.combine(week_end, time(19), tzinfo=timezone).astimezone(UTC)
    accepted = 0
    settings = get_settings()
    public_base = (settings.frontend_url or "").rstrip("/")
    api_base = (settings.app_public_url or public_base).rstrip("/")
    local_date = current.astimezone(timezone).date()

    for professional in await _eligible_professionals(db, current, closing_at):
        delivery = await db.scalar(
            select(WeeklySummaryEmailDelivery).where(
                WeeklySummaryEmailDelivery.professional_id == professional.id,
                WeeklySummaryEmailDelivery.week_start == week_start,
            )
        )
        if delivery is None:
            snapshot = await build_weekly_summary_snapshot(
                db, professional.id, week_start, week_end, local_date
            )
            delivery = WeeklySummaryEmailDelivery(
                professional_id=professional.id,
                week_start=week_start,
                week_end=week_end,
                snapshot=snapshot,
                status="queued" if _has_summary_activity(snapshot) else "skipped",
                skip_reason=None if _has_summary_activity(snapshot) else "no_activity",
            )
            db.add(delivery)
            await db.commit()
        if delivery.status in {"sent", "skipped"}:
            continue
        if (
            delivery.status == "failed"
            and delivery.next_retry_at is None
            and delivery.attempt_count > 0
        ):
            continue
        if delivery.next_retry_at is not None:
            retry_at = delivery.next_retry_at
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            if retry_at > current:
                continue

        token = create_weekly_summary_unsubscribe_token(professional.id)
        unsubscribe_url = (
            f"{api_base}/api/v1/notifications/weekly-summary/unsubscribe"
            f"?token={quote(token, safe='')}"
        )
        rendered = weekly_summary_email(
            week_start=delivery.week_start,
            week_end=delivery.week_end,
            appointments=delivery.snapshot["appointments"],
            finance=delivery.snapshot["finance"],
            agenda_url=f"{public_base}/agenda",
            finance_url=f"{public_base}/financeiro",
            support_url=f"{public_base}/suporte",
            unsubscribe_url=unsubscribe_url,
        )
        delivery.status = "sending"
        delivery.attempt_count += 1
        delivery.last_error = None
        await db.commit()
        try:
            provider_message_id = await asyncio.to_thread(
                send_email,
                to_email=professional.email,
                subject=rendered.subject,
                html=rendered.html,
                text=rendered.text,
                headers={
                    "List-Unsubscribe": f"<{unsubscribe_url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
                idempotency_key=(
                    f"weekly-summary/{professional.id}/{week_start.isoformat()}"
                ),
            )
            if not provider_message_id:
                raise RuntimeError("email_sending_unavailable")
        except Exception as exc:
            delivery.status = "failed"
            delivery.last_error = type(exc).__name__
            delivery.next_retry_at = _retry_at(current, delivery.attempt_count)
            await db.commit()
            continue

        delivery.status = "sent"
        delivery.provider_message_id = provider_message_id
        delivery.accepted_at = current
        delivery.next_retry_at = None
        accepted += 1
        await db.commit()

    return accepted
