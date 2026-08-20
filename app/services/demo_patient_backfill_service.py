from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient
from app.models.professional import Professional
from app.services.demo_patient_service import ensure_demo_patient_clinical_history


@dataclass(frozen=True, slots=True)
class DemoPatientBackfillReport:
    demo_patients: int
    changed_patients: int
    anamnese_entries: int
    evolutions: int
    assessments: int
    goals: int
    domain_snapshots: int

    @property
    def total_records(self) -> int:
        return (
            self.anamnese_entries
            + self.evolutions
            + self.assessments
            + self.goals
            + self.domain_snapshots
        )


async def enrich_all_demo_patients(
    db: AsyncSession,
    *,
    expected_count: int | None = None,
) -> DemoPatientBackfillReport:
    """Enrich every demo patient; the caller decides whether to commit or roll back."""
    stmt = (
        select(Patient, Professional)
        .join(Professional, Professional.id == Patient.professional_id)
        .where(Patient.is_demo.is_(True))
        .order_by(Patient.id)
    )
    if expected_count is not None:
        stmt = stmt.with_for_update(of=Patient)

    rows = (await db.execute(stmt)).all()
    demo_count = len(rows)
    if expected_count is not None and demo_count != expected_count:
        raise RuntimeError(
            f"Contagem de pacientes demo divergente: esperados {expected_count}, "
            f"encontrados {demo_count}"
        )

    changed_patients = 0
    anamnese_entries = 0
    evolutions = 0
    assessments = 0
    goals = 0
    domain_snapshots = 0
    for patient, professional in rows:
        changes = await ensure_demo_patient_clinical_history(
            db,
            professional,
            patient,
        )
        if changes.total:
            changed_patients += 1
        anamnese_entries += changes.anamnese_entries
        evolutions += changes.evolutions
        assessments += changes.assessments
        goals += changes.goals
        domain_snapshots += changes.domain_snapshots

    return DemoPatientBackfillReport(
        demo_patients=demo_count,
        changed_patients=changed_patients,
        anamnese_entries=anamnese_entries,
        evolutions=evolutions,
        assessments=assessments,
        goals=goals,
        domain_snapshots=domain_snapshots,
    )
