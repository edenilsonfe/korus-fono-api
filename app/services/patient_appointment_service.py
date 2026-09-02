import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.models.patient import Patient
from app.models.google_calendar import GoogleCalendarSyncRecord
from app.services.whatsapp_appointment_outbox import create_appointment_event_logs
from app.services.google_calendar_service import queue_appointment_sync

CANCELLABLE_APPOINTMENT_STATUSES = ("pendente", "confirmado")


@dataclass(frozen=True)
class InactivePatientAppointmentInventory:
    patient_count: int
    professional_count: int
    appointment_count: int
    pending_count: int
    confirmed_count: int
    manifest_sha256: str


def appointment_occurs_in_future(appointment: Appointment, clinic_now: datetime) -> bool:
    scheduled_at = datetime.combine(appointment.date, appointment.time).replace(
        tzinfo=clinic_now.tzinfo
    )
    return scheduled_at > clinic_now


async def _load_inactive_patient_appointment_rows(
    db: AsyncSession,
    clinic_now: datetime,
    *,
    lock: bool,
) -> list[tuple[Appointment, str]]:
    statement = (
        select(Appointment, Patient.name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Patient.status == "inativo",
            Appointment.date >= clinic_now.date(),
            Appointment.status.in_(CANCELLABLE_APPOINTMENT_STATUSES),
        )
        .order_by(Appointment.date.asc(), Appointment.time.asc(), Appointment.id.asc())
    )
    if lock:
        statement = statement.with_for_update(of=Appointment)
    rows = (await db.execute(statement)).all()
    return [
        (appointment, patient_name)
        for appointment, patient_name in rows
        if appointment_occurs_in_future(appointment, clinic_now)
    ]


def _inactive_patient_appointment_inventory(
    rows: list[tuple[Appointment, str]],
) -> InactivePatientAppointmentInventory:
    appointments = [appointment for appointment, _patient_name in rows]
    manifest = "\n".join(sorted(str(appointment.id) for appointment in appointments))
    return InactivePatientAppointmentInventory(
        patient_count=len({appointment.patient_id for appointment in appointments}),
        professional_count=len(
            {appointment.professional_id for appointment in appointments}
        ),
        appointment_count=len(appointments),
        pending_count=sum(
            appointment.status == "pendente" for appointment in appointments
        ),
        confirmed_count=sum(
            appointment.status == "confirmado" for appointment in appointments
        ),
        manifest_sha256=hashlib.sha256(manifest.encode("ascii")).hexdigest(),
    )


async def get_inactive_patient_appointment_inventory(
    db: AsyncSession,
    *,
    clinic_now: datetime | None = None,
) -> InactivePatientAppointmentInventory:
    """Return aggregate-only inventory for the guarded historical cleanup."""
    if clinic_now is None:
        clinic_now = datetime.now(ZoneInfo(get_settings().clinic_timezone))
    rows = await _load_inactive_patient_appointment_rows(db, clinic_now, lock=False)
    return _inactive_patient_appointment_inventory(rows)


async def backfill_inactive_patient_appointments(
    db: AsyncSession,
    *,
    expected_patient_count: int,
    expected_appointment_count: int,
    expected_manifest_sha256: str,
    clinic_now: datetime | None = None,
) -> tuple[
    InactivePatientAppointmentInventory,
    list[NotificationMessageLog],
    list[GoogleCalendarSyncRecord],
]:
    """Cancel the guarded snapshot of future appointments for inactive patients."""
    if clinic_now is None:
        clinic_now = datetime.now(ZoneInfo(get_settings().clinic_timezone))
    rows = await _load_inactive_patient_appointment_rows(db, clinic_now, lock=True)
    inventory = _inactive_patient_appointment_inventory(rows)
    if inventory.patient_count != expected_patient_count:
        raise RuntimeError(
            "A contagem de pacientes mudou: "
            f"esperados {expected_patient_count}, encontrados {inventory.patient_count}"
        )
    if inventory.appointment_count != expected_appointment_count:
        raise RuntimeError(
            "A contagem de agendamentos mudou: "
            f"esperados {expected_appointment_count}, encontrados {inventory.appointment_count}"
        )
    if inventory.manifest_sha256 != expected_manifest_sha256.strip().lower():
        raise RuntimeError("O conjunto de agendamentos mudou desde o preview")

    for appointment, _patient_name in rows:
        appointment.status = "cancelado"
    event_logs: list[NotificationMessageLog] = []

    google_records: list[GoogleCalendarSyncRecord] = []
    for appointment, patient_name in rows:
        record = await queue_appointment_sync(db, appointment, patient_name)
        if record:
            google_records.append(record)
    await db.flush()
    return inventory, event_logs, google_records


async def cancel_future_patient_appointments(
    db: AsyncSession,
    *,
    professional_id: UUID,
    patient_id: UUID,
    notify_via_whatsapp: bool = True,
    clinic_now: datetime | None = None,
) -> tuple[
    list[Appointment],
    list[NotificationMessageLog],
    list[GoogleCalendarSyncRecord],
]:
    """Cancel eligible future appointments and persist enabled integrations atomically."""
    if clinic_now is None:
        clinic_now = datetime.now(ZoneInfo(get_settings().clinic_timezone))

    result = await db.execute(
        select(Appointment, Patient.name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Appointment.professional_id == professional_id,
            Appointment.patient_id == patient_id,
            Appointment.date >= clinic_now.date(),
            Appointment.status.in_(CANCELLABLE_APPOINTMENT_STATUSES),
        )
        .order_by(Appointment.date.asc(), Appointment.time.asc())
    )
    rows = result.all()
    appointments = [
        appointment
        for appointment, _patient_name in rows
        if appointment_occurs_in_future(appointment, clinic_now)
    ]
    for appointment in appointments:
        appointment.status = "cancelado"

    event_logs = (
        await create_appointment_event_logs(db, appointments, "cancelled")
        if notify_via_whatsapp
        else []
    )
    google_records = []
    patient_name = rows[0][1] if rows else "Paciente"
    for appointment in appointments:
        record = await queue_appointment_sync(db, appointment, patient_name)
        if record:
            google_records.append(record)
    await db.commit()
    return appointments, event_logs, google_records
