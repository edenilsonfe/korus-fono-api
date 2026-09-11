from datetime import date
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AVATAR_COLORS
from app.core.deps import get_patient_for_professional, require_verified_professional
from app.core.diagnosis_catalog import diagnosis_labels, validate_diagnosis_keys
from app.core.utils import (
    calculate_age,
    guardian_label,
    utcnow,
)
from app.db.session import get_db
from app.models.anamnese import AnamneseEntry
from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.attachment import Attachment
from app.models.care_team import PatientSharingConsentEvent
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.home_program import HomeProgram
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.patient_record_export import PatientRecordExport
from app.models.professional import Professional
from app.models.session import Session
from app.schemas.common import PaginatedResponse
from app.schemas.patient import (
    BulkAppointmentCancellationResponse,
    CaregiverCreate,
    CaregiverResponse,
    CaregiverUpdate,
    PatientAccessResponse,
    PatientCreate,
    PatientDetail,
    PatientSummary,
    PatientUpdate,
    TherapyPlanUpdate,
)
from app.services.care_team_service import record_access_event
from app.services.feature_flag_service import FeatureFlagService
from app.services.google_calendar_service import (
    dispatch_sync_records,
    queue_appointment_sync,
)
from app.services.home_program_access import revoke_grants_for_caregiver
from app.services.patient import (
    get_patient_aggregates,
    get_patient_aggregates_batch,
)
from app.services.patient_access import (
    FEATURE_KEY,
    ROLE_PERMISSIONS,
    PatientAccess,
    list_shared_patient_accesses,
    resolve_patient_access,
)
from app.services.patient_appointment_service import cancel_future_patient_appointments
from app.services.school_report_delivery_service import (
    revoke_school_deliveries_for_caregiver,
)
from app.services.timeline import create_timeline_event
from app.services.whatsapp_queue import enqueue_whatsapp_appointment_event_log

router = APIRouter(prefix="/patients", tags=["patients"])


def _caregiver_response(c: Caregiver) -> CaregiverResponse:
    return CaregiverResponse(
        id=str(c.id),
        name=c.name,
        relation=c.relation,
        phone=c.phone,
        email=c.email,
        notes=c.notes,
        is_primary=c.is_primary,
        whatsapp_opt_in=c.whatsapp_opt_in,
    )


async def _get_caregiver_for_patient(
    patient_id: UUID,
    caregiver_id: UUID,
    professional: Professional,
    db: AsyncSession,
) -> Caregiver:
    await get_patient_for_professional(patient_id, professional, db)
    result = await db.execute(
        select(Caregiver).where(Caregiver.id == caregiver_id, Caregiver.patient_id == patient_id)
    )
    caregiver = result.scalar_one_or_none()
    if not caregiver:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Responsável não encontrado")
    return caregiver


async def _set_primary_caregiver(db: AsyncSession, patient_id: UUID, caregiver_id: UUID) -> None:
    existing = (
        await db.execute(select(Caregiver).where(Caregiver.patient_id == patient_id))
    ).scalars().all()
    for item in existing:
        item.is_primary = item.id == caregiver_id


def _summary_from_aggregates(
    patient: Patient,
    professional: Professional,
    aggregates: dict,
    caregivers: list[Caregiver],
    access: PatientAccess | None = None,
) -> PatientSummary:
    shared = access is not None and not access.is_owner
    gl = "" if shared else guardian_label(caregivers)
    last = aggregates["last_session"]
    keys = patient.diagnosis_keys or []
    labels = diagnosis_labels(keys, professional.specialty_key)
    return PatientSummary(
        id=str(patient.id),
        name=patient.name,
        age=calculate_age(patient.birth_date),
        birth_date=patient.birth_date.isoformat(),
        address=None if shared else patient.address,
        notes=None if shared else patient.notes,
        guardian=gl,
        guardian_label=gl,
        diagnoses=labels,
        diagnosis_keys=keys,
        therapist=professional.name,
        status=patient.status,
        start_date=patient.start_date.isoformat(),
        last_session=last.isoformat() if last else None,
        sessions_count=aggregates["sessions_count"],
        protocols_done=aggregates["protocols_done"],
        goals_achieved=aggregates["goals_achieved"],
        total_goals=aggregates["total_goals"],
        avatar_color=patient.avatar_color,
        is_demo=patient.is_demo,
        therapy_plan_content=patient.therapy_plan_content,
        therapy_plan_updated_at=patient.therapy_plan_updated_at.isoformat()
        if patient.therapy_plan_updated_at
        else None,
        access=_access_response(access),
    )


def _access_response(access: PatientAccess | None) -> PatientAccessResponse | None:
    if access is None:
        return None
    return PatientAccessResponse(
        role=access.role,
        is_owner=access.is_owner,
        permissions=sorted(access.permissions),
    )


async def _build_summary(db: AsyncSession, patient: Patient, professional: Professional) -> PatientSummary:
    aggregates = await get_patient_aggregates(db, patient.id)
    caregivers_result = await db.execute(select(Caregiver).where(Caregiver.patient_id == patient.id))
    caregivers = caregivers_result.scalars().all()
    return _summary_from_aggregates(patient, professional, aggregates, caregivers)


async def _build_detail(
    db: AsyncSession,
    patient: Patient,
    professional: Professional,
    include: set[str],
) -> PatientDetail:
    from app.services.patient_record import build_patient_detail

    return await build_patient_detail(db, patient, professional, include)


@router.get("", response_model=PaginatedResponse[PatientSummary])
async def list_patients(
    status_filter: str | None = Query(None, alias="status"),
    diagnosis_key: str | None = Query(None, alias="diagnosisKey"),
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    shared_accesses = await list_shared_patient_accesses(db, professional)
    query = select(Patient).where(
        or_(
            Patient.professional_id == professional.id,
            Patient.id.in_(shared_accesses),
        )
    )
    if status_filter:
        query = query.where(Patient.status == status_filter)
    if diagnosis_key:
        query = query.where(Patient.diagnosis_keys.contains([diagnosis_key]))
    if q:
        query = query.where(Patient.name.ilike(f"%{q}%"))

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(query.order_by(Patient.name.asc()).offset((page - 1) * limit).limit(limit))
    patients = result.scalars().all()

    patient_ids = [p.id for p in patients]
    owner_ids = {p.professional_id for p in patients}
    owners = (
        (await db.execute(select(Professional).where(Professional.id.in_(owner_ids))))
        .scalars()
        .all()
    )
    owners_by_id = {owner.id: owner for owner in owners}
    own_feature_enabled = await FeatureFlagService(db).is_enabled(
        professional, FEATURE_KEY
    )
    aggregates_map = await get_patient_aggregates_batch(db, patient_ids)
    caregivers_result = await db.execute(select(Caregiver).where(Caregiver.patient_id.in_(patient_ids)))
    caregivers_by_patient: dict[UUID, list[Caregiver]] = {}
    for caregiver in caregivers_result.scalars().all():
        caregivers_by_patient.setdefault(caregiver.patient_id, []).append(caregiver)

    items = []
    for item in patients:
        access = shared_accesses.get(item.id)
        if item.professional_id == professional.id and own_feature_enabled:
            access = PatientAccess(
                item,
                "coordinator",
                True,
                ROLE_PERMISSIONS["coordinator"],
            )
        items.append(
            _summary_from_aggregates(
                item,
                owners_by_id[item.professional_id],
                aggregates_map[item.id],
                caregivers_by_patient.get(item.id, []),
                access,
            )
        )
    return PaginatedResponse(items=items, total=total or 0, page=page, limit=limit)


@router.post("", response_model=PatientSummary, status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: PatientCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    try:
        validate_diagnosis_keys(body.diagnosis_keys, professional.specialty_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    count = await db.scalar(select(func.count()).select_from(Patient).where(Patient.professional_id == professional.id))
    patient = Patient(
        professional_id=professional.id,
        name=body.name,
        birth_date=body.birth_date,
        address=body.address,
        notes=body.notes,
        diagnosis_keys=body.diagnosis_keys,
        status=body.status,
        start_date=date.today(),
        avatar_color=AVATAR_COLORS[(count or 0) % len(AVATAR_COLORS)],
    )
    db.add(patient)
    await db.flush()

    for i, g in enumerate(body.guardians):
        relation = g.relation or (g.contact.split(" — ")[0] if g.contact and " — " in g.contact else "Responsável")
        name = g.name or (g.contact.split(" — ")[1] if g.contact and " — " in g.contact else g.name)
        caregiver = Caregiver(
            patient_id=patient.id,
            name=name,
            relation=relation,
            phone=g.phone,
            email=g.email,
            is_primary=i == 0,
            whatsapp_opt_in=g.whatsapp_opt_in,
        )
        db.add(caregiver)

    await create_timeline_event(
        db,
        patient_id=patient.id,
        professional_id=professional.id,
        event_type="meta",
        title="Paciente cadastrado",
        description="Início do acompanhamento na clínica",
    )
    await db.flush()
    if professional.onboarding_completed_at is None:
        professional.onboarding_completed_at = utcnow()
    return await _build_summary(db, patient, professional)


@router.post("/{patient_id}/caregivers", response_model=CaregiverResponse, status_code=status.HTTP_201_CREATED)
async def create_caregiver(
    patient_id: UUID,
    body: CaregiverCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_patient_for_professional(patient_id, professional, db)
    existing = (
        await db.execute(select(Caregiver).where(Caregiver.patient_id == patient_id))
    ).scalars().all()

    relation = body.relation.strip() or "Responsável"
    is_primary = body.is_primary if existing else True

    caregiver = Caregiver(
        patient_id=patient_id,
        name=body.name.strip(),
        relation=relation,
        phone=body.phone.strip(),
        email=body.email.strip(),
        notes=body.notes.strip(),
        is_primary=is_primary,
        whatsapp_opt_in=body.whatsapp_opt_in,
    )
    db.add(caregiver)
    await db.flush()

    if is_primary:
        await _set_primary_caregiver(db, patient_id, caregiver.id)

    return _caregiver_response(caregiver)


@router.patch("/{patient_id}/caregivers/{caregiver_id}", response_model=CaregiverResponse)
async def update_caregiver(
    patient_id: UUID,
    caregiver_id: UUID,
    body: CaregiverUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    caregiver = await _get_caregiver_for_patient(patient_id, caregiver_id, professional, db)
    data = body.model_dump(exclude_unset=True)

    if "name" in data and data["name"] is not None:
        caregiver.name = data["name"].strip()
    if "relation" in data and data["relation"] is not None:
        caregiver.relation = data["relation"].strip() or "Responsável"
    if "phone" in data and data["phone"] is not None:
        caregiver.phone = data["phone"].strip()
    if "email" in data and data["email"] is not None:
        caregiver.email = data["email"].strip()
    if "notes" in data and data["notes"] is not None:
        caregiver.notes = data["notes"].strip()
    if "whatsapp_opt_in" in data and data["whatsapp_opt_in"] is not None:
        caregiver.whatsapp_opt_in = data["whatsapp_opt_in"]

    if data.get("is_primary") is True:
        await _set_primary_caregiver(db, patient_id, caregiver.id)
    elif data.get("is_primary") is False and caregiver.is_primary:
        caregiver.is_primary = False
        remaining = (
            await db.execute(
                select(Caregiver).where(Caregiver.patient_id == patient_id, Caregiver.id != caregiver_id)
            )
        ).scalars().all()
        if remaining and not any(c.is_primary for c in remaining):
            remaining[0].is_primary = True

    await db.flush()
    return _caregiver_response(caregiver)


@router.delete("/{patient_id}/caregivers/{caregiver_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_caregiver(
    patient_id: UUID,
    caregiver_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    caregiver = await _get_caregiver_for_patient(patient_id, caregiver_id, professional, db)
    was_primary = caregiver.is_primary
    # F20: school deliveries derived from this caregiver's authorization are
    # revoked before the row disappears; the historical snapshot is preserved.
    await revoke_school_deliveries_for_caregiver(
        db, patient_id=patient_id, caregiver_id=caregiver_id
    )
    # F16: home program grants issued to this caregiver are revoked before the
    # row disappears; the author snapshot is preserved (FK would SET NULL).
    await revoke_grants_for_caregiver(
        db,
        patient_id=patient_id,
        caregiver_id=caregiver_id,
        actor=professional,
    )
    await db.delete(caregiver)
    await db.flush()

    if was_primary:
        remaining = (
            await db.execute(select(Caregiver).where(Caregiver.patient_id == patient_id))
        ).scalars().all()
        if remaining:
            remaining[0].is_primary = True


@router.get("/{patient_id}", response_model=PatientDetail)
async def get_patient(
    patient_id: UUID,
    include: str | None = Query(None),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado"
        )
    if patient.professional_id == professional.id:
        owner = professional
        access = await resolve_patient_access(db, patient_id, professional)
    else:
        access = await resolve_patient_access(db, patient_id, professional)
        if access is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Paciente não encontrado",
            )
        owner = await db.get(Professional, patient.professional_id)
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Paciente não encontrado",
            )
    include_set = set(include.split(",")) if include else set()
    detail = await _build_detail(db, patient, owner, include_set)
    detail.access = _access_response(access)
    if access is not None and not access.is_owner:
        detail.address = None
        detail.notes = None
        detail.guardian = ""
        detail.guardian_label = ""
        detail.caregivers = []
        record_access_event(
            db,
            patient_id=patient.id,
            actor=professional,
            actor_role=access.role,
            action="patient_record_opened",
            resource_type="patient",
            resource_id=patient.id,
        )
    return detail


@router.patch("/{patient_id}", response_model=PatientSummary)
async def update_patient(
    patient_id: UUID,
    body: PatientUpdate,
    background_tasks: BackgroundTasks,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    previous_status = patient.status
    data = body.model_dump(exclude_unset=True)
    if "diagnosis_keys" in data and data["diagnosis_keys"] is not None:
        try:
            validate_diagnosis_keys(data["diagnosis_keys"], professional.specialty_key)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    for field, value in data.items():
        setattr(patient, field, value)
    if previous_status != "inativo" and patient.status == "inativo":
        _appointments, _event_logs, google_records = await cancel_future_patient_appointments(
            db,
            professional_id=professional.id,
            patient_id=patient.id,
            notify_via_whatsapp=False,
        )
        if google_records:
            background_tasks.add_task(
                dispatch_sync_records, [record.id for record in google_records]
            )
    else:
        await db.flush()
    return await _build_summary(db, patient, professional)


@router.patch(
    "/{patient_id}/appointments/cancel-future",
    response_model=BulkAppointmentCancellationResponse,
)
async def cancel_future_appointments(
    patient_id: UUID,
    background_tasks: BackgroundTasks,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    appointments, event_logs, google_records = await cancel_future_patient_appointments(
        db,
        professional_id=professional.id,
        patient_id=patient.id,
    )
    for event_log in event_logs:
        background_tasks.add_task(enqueue_whatsapp_appointment_event_log, event_log.id)
    if google_records:
        background_tasks.add_task(
            dispatch_sync_records, [record.id for record in google_records]
        )
    return BulkAppointmentCancellationResponse(cancelled_count=len(appointments))


@router.put("/{patient_id}/therapy-plan", response_model=PatientSummary)
async def update_therapy_plan(
    patient_id: UUID,
    body: TherapyPlanUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    patient.therapy_plan_content = body.content
    patient.therapy_plan_updated_at = utcnow()
    await create_timeline_event(
        db,
        patient_id=patient.id,
        professional_id=professional.id,
        event_type="meta",
        title="Plano terapêutico atualizado",
        description="Documento do plano terapêutico foi atualizado no prontuário",
    )
    await db.flush()
    return await _build_summary(db, patient, professional)


@router.delete("/{patient_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_patient(
    patient_id: UUID,
    background_tasks: BackgroundTasks,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient = await get_patient_for_professional(patient_id, professional, db)
    sharing_history = await db.scalar(
        select(PatientSharingConsentEvent.id)
        .where(PatientSharingConsentEvent.patient_id == patient.id)
        .limit(1)
    )
    program_history = await db.scalar(
        select(InterventionProgram.id)
        .where(InterventionProgram.patient_id == patient.id)
        .limit(1)
    )
    clinical_history = program_history is not None
    for model in (Session, Assessment, Goal, Evolution, AnamneseEntry, Attachment):
        if await db.scalar(select(model.id).where(model.patient_id == patient.id).limit(1)):
            clinical_history = True
            break
    if sharing_history is not None or clinical_history:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Paciente com histórico clínico ou de compartilhamento "
                "não pode ser excluído; "
                "altere o status para inativo"
            ),
        )
    export_history = await db.scalar(
        select(PatientRecordExport.id)
        .where(PatientRecordExport.patient_id == patient.id)
        .limit(1)
    )
    if export_history is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Paciente com histórico de exportação do prontuário "
                "não pode ser excluído; "
                "altere o status para inativo"
            ),
        )
    home_program_history = await db.scalar(
        select(HomeProgram.id)
        .where(HomeProgram.patient_id == patient.id)
        .limit(1)
    )
    if home_program_history is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Paciente com histórico de programa de casa "
                "não pode ser excluído; "
                "altere o status para inativo"
            ),
        )
    if patient.is_demo:
        real_patient_id = await db.scalar(
            select(Patient.id)
            .where(
                Patient.professional_id == professional.id,
                Patient.is_demo.is_(False),
            )
            .limit(1)
        )
        if real_patient_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "O paciente demonstração faz parte dos primeiros passos "
                    "e não pode ser removido agora"
                ),
            )
    appointments = list(
        (
            await db.execute(
                select(Appointment).where(
                    Appointment.professional_id == professional.id,
                    Appointment.patient_id == patient.id,
                )
            )
        ).scalars()
    )
    google_records = []
    for appointment in appointments:
        record = await queue_appointment_sync(
            db, appointment, patient.name, operation="delete"
        )
        if record:
            google_records.append(record)
    await db.delete(patient)
    await db.commit()
    if google_records:
        background_tasks.add_task(
            dispatch_sync_records, [record.id for record in google_records]
        )
