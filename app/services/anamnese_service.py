"""Anamnese draft upsert + complete lock."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.models.anamnese import AnamneseEntry
from app.models.patient import Patient
from app.schemas.prontuario import AnamneseDocumentResponse, AnamneseEntryInput, AnamneseEntryResponse

ANAMNESE_STATUS_DRAFT = "draft"
ANAMNESE_STATUS_COMPLETED = "completed"


def _entry_response(entry: AnamneseEntry) -> AnamneseEntryResponse:
    return AnamneseEntryResponse(
        id=str(entry.id),
        patient_id=str(entry.patient_id),
        section=entry.section,
        value=entry.value,
    )


async def list_entries(db: AsyncSession, patient_id: UUID) -> list[AnamneseEntry]:
    result = await db.execute(
        select(AnamneseEntry)
        .where(AnamneseEntry.patient_id == patient_id)
        .order_by(AnamneseEntry.section.asc())
    )
    return list(result.scalars().all())


def document_response(patient: Patient, entries: list[AnamneseEntry]) -> AnamneseDocumentResponse:
    completed_at = patient.anamnese_completed_at.isoformat() if patient.anamnese_completed_at else None
    return AnamneseDocumentResponse(
        status=patient.anamnese_status or ANAMNESE_STATUS_DRAFT,
        completed_at=completed_at,
        entries=[_entry_response(e) for e in entries],
    )


def assert_editable(patient: Patient) -> None:
    if patient.anamnese_status == ANAMNESE_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Anamnese concluída — não é possível editar.",
        )


async def _lock_patient(db: AsyncSession, patient_id: UUID) -> Patient:
    patient = await db.scalar(
        select(Patient)
        .where(Patient.id == patient_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado")
    return patient


async def upsert_entries(
    db: AsyncSession,
    *,
    patient_id: UUID,
    entries: list[AnamneseEntryInput],
) -> list[AnamneseEntry]:
    # All writers share the patient-row lock. Re-read the status after waiting
    # for the lock so a stale router object cannot edit a completed document.
    patient = await _lock_patient(db, patient_id)
    assert_editable(patient)
    for item in entries:
        section = item.section.strip()
        if not section:
            continue
        value = item.value.strip()
        result = await db.execute(
            select(AnamneseEntry).where(
                AnamneseEntry.patient_id == patient_id,
                AnamneseEntry.section == section,
            )
        )
        entry = result.scalar_one_or_none()
        if entry:
            entry.value = value
        else:
            db.add(AnamneseEntry(patient_id=patient_id, section=section, value=value))
    await db.flush()
    return await list_entries(db, patient_id)


def has_non_empty_content(entries: list[AnamneseEntry]) -> bool:
    return any(e.value.strip() for e in entries)


async def complete_anamnese(
    db: AsyncSession,
    *,
    patient: Patient,
    entries: list[AnamneseEntryInput] | None,
) -> AnamneseDocumentResponse:
    patient = await _lock_patient(db, patient.id)
    assert_editable(patient)

    if entries:
        saved = await upsert_entries(db, patient_id=patient.id, entries=entries)
    else:
        saved = await list_entries(db, patient.id)

    if not has_non_empty_content(saved):
        raise HTTPException(
            status_code=422,
            detail="Preencha pelo menos uma seção antes de concluir a anamnese.",
        )

    patient.anamnese_status = ANAMNESE_STATUS_COMPLETED
    patient.anamnese_completed_at = utcnow()
    await db.flush()
    return document_response(patient, saved)
