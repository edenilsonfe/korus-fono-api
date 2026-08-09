from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo_patient import DEMO_AVATAR_COLOR, DEMO_PATIENT_NAME, demo_patient_birth_date
from app.models.patient import Patient
from app.models.professional import Professional


async def ensure_demo_patient(
    db: AsyncSession,
    professional: Professional,
) -> Patient:
    """Return the professional demo patient, recreating it safely when missing."""
    await db.execute(
        select(Professional.id)
        .where(Professional.id == professional.id)
        .with_for_update()
    )
    existing = await db.scalar(
        select(Patient)
        .where(
            Patient.professional_id == professional.id,
            Patient.is_demo.is_(True),
        )
        .order_by(Patient.created_at.asc())
        .limit(1)
    )
    if existing is not None:
        return existing

    patient = Patient(
        professional_id=professional.id,
        name=DEMO_PATIENT_NAME,
        birth_date=demo_patient_birth_date(),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color=DEMO_AVATAR_COLOR,
        is_demo=True,
    )
    db.add(patient)
    await db.flush()
    return patient
