"""Pure helpers for recurring appointment series slot generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from dateutil import rrule

MAX_RECURRENT_DAYS = 90


@dataclass(frozen=True)
class AppointmentSlot:
    start_date: date
    start_time: time
    end_time: time
    duration: int


@dataclass(frozen=True)
class WeekdaySlotRule:
    weekday: int
    start_time: time
    duration: int


def end_time_from_duration(start: time, duration: int) -> time:
    start_dt = datetime.combine(date.today(), start)
    return (start_dt + timedelta(minutes=duration)).time()


def _frequency_dates(
    frequency: str,
    start_date: date,
    end_date: date,
    weekdays: list[int] | None = None,
) -> list[date]:
    if frequency == "personalizado":
        selected = weekdays or []
        if not selected:
            return [start_date]
        result: list[date] = []
        current = start_date
        while current <= end_date:
            if current.weekday() in selected:
                result.append(current)
            current += timedelta(days=1)
        return result

    if frequency == "semanal":
        dates = list(
            rrule.rrule(
                rrule.WEEKLY,
                dtstart=start_date,
                until=end_date,
            )
        )
    elif frequency == "quinzenal":
        dates = list(
            rrule.rrule(
                rrule.WEEKLY,
                dtstart=start_date,
                until=end_date,
                interval=2,
            )
        )
    elif frequency == "mensal":
        dates = list(
            rrule.rrule(
                rrule.MONTHLY,
                dtstart=start_date,
                until=end_date,
            )
        )
    else:
        dates = [datetime.combine(start_date, datetime.min.time())]

    result: list[date] = []
    for session_date in dates:
        appointment_date = session_date.date() if hasattr(session_date, "date") else session_date
        if start_date <= appointment_date <= end_date:
            result.append(appointment_date)
    return result


def rules_by_weekday(rules: list[WeekdaySlotRule] | None) -> dict[int, WeekdaySlotRule]:
    if not rules:
        return {}
    return {rule.weekday: rule for rule in rules}


def iter_recurring_child_slots(
    frequency: str | None,
    start_date: date,
    end_date: date,
    start_time: time,
    end_time: time,
    weekdays: list[int] | None = None,
    *,
    duration: int | None = None,
    weekday_rules: list[WeekdaySlotRule] | None = None,
    include_first: bool = False,
) -> Iterable[AppointmentSlot]:
    """Yield slots; series editing includes the first matching occurrence."""
    if not end_date:
        return

    default_duration = duration if duration is not None else 50
    by_weekday = rules_by_weekday(weekday_rules)
    dates = _frequency_dates(frequency or "semanal", start_date, end_date, weekdays)
    for appointment_date in dates if include_first else dates[1:]:
        rule = by_weekday.get(appointment_date.weekday())
        slot_time = rule.start_time if rule else start_time
        slot_duration = rule.duration if rule else default_duration
        yield AppointmentSlot(
            start_date=appointment_date,
            start_time=slot_time,
            end_time=end_time_from_duration(slot_time, slot_duration),
            duration=slot_duration,
        )


def slot_matches(
    slot: AppointmentSlot,
    start_date: date,
    start_time: time,
    end_time: time,
) -> bool:
    return (
        slot.start_date == start_date
        and slot.start_time == start_time
        and slot.end_time == end_time
    )


def validate_recurrent_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("Data fim deve ser posterior ou igual à data início")
    if (end_date - start_date).days > MAX_RECURRENT_DAYS:
        raise ValueError(f"Intervalo máximo de recorrência é {MAX_RECURRENT_DAYS} dias")
