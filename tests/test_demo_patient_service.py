from datetime import date, timedelta

from sqlalchemy import func, select

from app.models.anamnese import AnamneseEntry
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import ClinicalDomainSnapshot, Goal
from app.models.patient import Patient
from app.services.ai_context import build_context
from app.services.demo_patient_service import ensure_demo_patient_clinical_history


async def test_demo_history_is_idempotent_and_preserves_existing_chart_data(
    db_session,
    professional,
):
    original_start_date = date.today() - timedelta(days=12)
    patient = Patient(
        professional_id=professional.id,
        name="Paciente demonstração",
        birth_date=date.today() - timedelta(days=730),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=original_start_date,
        avatar_color="oklch(0.58 0.12 205)",
        is_demo=True,
    )
    db_session.add(patient)
    await db_session.flush()
    db_session.add(
        AnamneseEntry(
            patient_id=patient.id,
            section="Gestação",
            value="Conteúdo personalizado que não pode ser substituído.",
        )
    )
    await db_session.flush()

    first = await ensure_demo_patient_clinical_history(
        db_session,
        professional,
        patient,
    )
    second = await ensure_demo_patient_clinical_history(
        db_session,
        professional,
        patient,
    )

    assert first.anamnese_entries == 7
    assert first.evolutions == 4
    assert first.assessments == 2
    assert first.goals == 2
    assert first.domain_snapshots == 9
    assert first.total == 24
    assert second.total == 0
    assert patient.start_date == original_start_date

    gestation = await db_session.scalar(
        select(AnamneseEntry.value).where(
            AnamneseEntry.patient_id == patient.id,
            AnamneseEntry.section == "Gestação",
        )
    )
    assert gestation == "Conteúdo personalizado que não pode ser substituído."

    async def count(model):
        return await db_session.scalar(
            select(func.count()).select_from(model).where(model.patient_id == patient.id)
        )

    assert await count(AnamneseEntry) == 8
    assert await count(Evolution) == 4
    assert await count(Assessment) == 2
    assert await count(Goal) == 2
    assert await count(ClinicalDomainSnapshot) == 9

    report_context = await build_context(
        db_session,
        patient.id,
        ["identity", "assessments", "evolutions", "goals", "anamnesis"],
        max_chars=12_000,
    )
    assert "### Anamnese" in report_context
    assert "### Evoluções" in report_context
    assert "### Avaliações" in report_context
    assert "### Metas terapêuticas" in report_context
    assert "Rastreio de Desenvolvimento Infantil" in report_context
    assert "Inventário Portage Operacionalizado" in report_context
