"""Atomic appointment completion linked to clinical and financial records."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.finance import PackageUsage, PatientPackage, ReceivableItem, ServiceOffering
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.finance import (
    AppointmentCompleteRequest,
    AppointmentCompleteResponse,
    ReceivableCreate,
    ReceivableItemCreate,
)
from app.services.clinical_activity import record_session
from app.services.financial_service import _audit, create_receivable_entity

# Brazil has observed UTC-03 without daylight saving time since 2019. Keeping
# this local timestamp explicit avoids depending on an OS tzdata package.
CLINIC_TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


async def _existing_completion(
    db: AsyncSession, appointment: Appointment, session: Session
) -> AppointmentCompleteResponse:
    item_result = await db.execute(
        select(ReceivableItem).where(ReceivableItem.appointment_id == appointment.id)
    )
    item = item_result.scalar_one_or_none()
    usage_result = await db.execute(
        select(PackageUsage).where(PackageUsage.appointment_id == appointment.id)
    )
    usage = usage_result.scalar_one_or_none()
    resolved_mode = "package" if usage else "individual" if item else "courtesy"
    return AppointmentCompleteResponse(
        appointment_id=appointment.id,
        session_id=session.id,
        receivable_id=item.receivable_id if item else None,
        package_usage_id=usage.id if usage else None,
        billing_mode=resolved_mode,
    )


async def complete_appointment(
    db: AsyncSession,
    professional: Professional,
    appointment_id: UUID,
    body: AppointmentCompleteRequest,
) -> AppointmentCompleteResponse:
    result = await db.execute(
        select(Appointment, Patient)
        .join(Patient, Appointment.patient_id == Patient.id)
        .where(
            Appointment.id == appointment_id,
            Appointment.professional_id == professional.id,
            Patient.professional_id == professional.id,
        )
        .with_for_update(of=Appointment)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
    appointment, patient = row
    if appointment.status == "cancelado":
        raise HTTPException(status_code=409, detail="Um atendimento cancelado não pode ser concluído")

    session_result = await db.execute(
        select(Session).where(
            Session.appointment_id == appointment.id,
            Session.professional_id == professional.id,
        )
    )
    existing_session = session_result.scalar_one_or_none()
    if appointment.status == "concluido":
        if not existing_session:
            raise HTTPException(
                status_code=409,
                detail="Atendimento concluído sem sessão clínica vinculada",
            )
        return await _existing_completion(db, appointment, existing_session)

    if body.billing_mode == "individual":
        if not body.service_id or not body.due_date or not body.payer_name:
            raise HTTPException(
                status_code=422,
                detail="Serviço, vencimento e pagador são obrigatórios na cobrança individual",
            )
        service_result = await db.execute(
            select(ServiceOffering).where(
                ServiceOffering.id == body.service_id,
                ServiceOffering.professional_id == professional.id,
                ServiceOffering.active.is_(True),
            )
        )
        service = service_result.scalar_one_or_none()
        if not service:
            raise HTTPException(status_code=404, detail="Serviço financeiro não encontrado")
    else:
        service = None

    if body.billing_mode == "package":
        if not body.patient_package_id:
            raise HTTPException(status_code=422, detail="Selecione o pacote do paciente")
        package_result = await db.execute(
            select(PatientPackage).where(
                PatientPackage.id == body.patient_package_id,
                PatientPackage.professional_id == professional.id,
                PatientPackage.patient_id == patient.id,
            ).with_for_update()
        )
        package = package_result.scalar_one_or_none()
        if not package:
            raise HTTPException(status_code=404, detail="Pacote do paciente não encontrado")
        if package.status != "active" or package.sessions_used >= package.sessions_included:
            raise HTTPException(status_code=409, detail="O pacote não possui sessões disponíveis")
        if appointment.date < package.started_on or appointment.date > package.expires_on:
            raise HTTPException(status_code=409, detail="O atendimento está fora da validade do pacote")
    else:
        package = None

    # The production column is PostgreSQL ARRAY; the lightweight SQLite test
    # compiler represents it as JSON but has no ARRAY bind processor.
    empty_objectives = literal_column("'[]'") if db.bind and db.bind.dialect.name == "sqlite" else []
    session = existing_session
    if session is None:
        session = Session(
            patient_id=patient.id,
            professional_id=professional.id,
            appointment_id=appointment.id,
            date=datetime.combine(appointment.date, appointment.time, tzinfo=CLINIC_TIMEZONE),
            duration=appointment.duration,
            type=appointment.type,
            objectives=empty_objectives,
            notes=body.notes,
        )
        db.add(session)
        await db.flush()
        await record_session(db, session=session, professional=professional)
    elif body.notes.strip():
        session.notes = body.notes.strip()

    receivable_id = None
    usage_id = None
    if body.billing_mode == "individual":
        assert service and body.due_date and body.payer_name
        receivable = await create_receivable_entity(
            db,
            professional.id,
            ReceivableCreate(
                patient_id=patient.id,
                payer_name=body.payer_name,
                payer_document=body.payer_document,
                description=service.name,
                issue_date=appointment.date,
                competence_date=appointment.date,
                due_date=body.due_date,
                category_id=service.category_id,
                origin="appointment",
                notes=body.notes,
                items=[
                    ReceivableItemCreate(
                        service_id=service.id,
                        description=service.name,
                        quantity=1,
                        unit_cents=service.price_cents,
                    )
                ],
            ),
            appointment_id=appointment.id,
            commit=False,
        )
        receivable_id = receivable.id
    elif body.billing_mode == "package":
        assert package
        usage = PackageUsage(
            patient_package_id=package.id,
            appointment_id=appointment.id,
            session_id=session.id,
            used_on=appointment.date,
        )
        db.add(usage)
        package.sessions_used += 1
        if package.sessions_used >= package.sessions_included:
            package.status = "completed"
        await db.flush()
        usage_id = usage.id

    appointment.status = "concluido"
    await _audit(
        db,
        professional.id,
        "appointment",
        appointment.id,
        "completed",
        {"billingMode": body.billing_mode},
    )
    await db.commit()
    return AppointmentCompleteResponse(
        appointment_id=appointment.id,
        session_id=session.id,
        receivable_id=receivable_id,
        package_usage_id=usage_id,
        billing_mode=body.billing_mode,
    )
