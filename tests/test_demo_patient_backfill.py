from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models.anamnese import AnamneseEntry
from app.models.patient import Patient
from app.services.demo_patient_backfill_service import enrich_all_demo_patients
from scripts.backfill_demo_patient_history import main


async def test_demo_backfill_previews_rolls_back_and_requires_exact_apply_count(
    db_session,
    professional,
):
    patient = Patient(
        professional_id=professional.id,
        name="Paciente demonstração",
        birth_date=date.today() - timedelta(days=730),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
        is_demo=True,
    )
    db_session.add(patient)
    await db_session.commit()

    preview = await enrich_all_demo_patients(db_session)
    await db_session.rollback()

    assert preview.demo_patients == 1
    assert preview.changed_patients == 1
    assert preview.anamnese_entries == 8
    assert preview.evolutions == 4
    assert preview.assessments == 2
    assert preview.goals == 2
    assert preview.domain_snapshots == 9
    assert preview.total_records == 25
    assert await db_session.scalar(select(func.count()).select_from(AnamneseEntry)) == 0

    with pytest.raises(RuntimeError, match="esperados 2.*encontrados 1"):
        await enrich_all_demo_patients(db_session, expected_count=2)
    await db_session.rollback()

    applied = await enrich_all_demo_patients(db_session, expected_count=1)
    await db_session.commit()
    assert applied == preview

    repeated = await enrich_all_demo_patients(db_session, expected_count=1)
    await db_session.rollback()
    assert repeated.demo_patients == 1
    assert repeated.changed_patients == 0
    assert repeated.total_records == 0


def test_demo_backfill_cli_requires_count_guard_for_apply(capsys):
    assert main(["--apply"]) == 2
    assert "--expected-count" in capsys.readouterr().err
