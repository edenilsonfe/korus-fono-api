import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.base import Base
from app.models.patient import Patient
from app.models.notification_settings import NotificationSettings
from app.services.legacy_clinic_import import (
    LegacyClinicImportError,
    apply_legacy_clinic_import,
    preview_legacy_clinic_import,
)


LEGACY_SQL = """\
-- Backup clinica 13
-- Gerado em 2026-08-18 09:26:54

-- Tabela: agendamento
INSERT INTO `agendamento` (`id`, `clinica_id`, `dt_inicial`, `dt_final`, `agendamento_pai_id`, `tipo_repeticao`, `repetir_ate`, `paciente_id`, `profissional_id`, `procedimento_id`, `valor_total`, `estado_agenda_id`, `evoluido`, `ativo`, `observacao`, `data_criacao`) VALUES (30, 13, '2026-06-10 08:00:00', '2026-06-10 08:40:00', NULL, NULL, NULL, 10, 155, 20, 150.00, 4, 'S', 'T', NULL, '2026-06-01 10:00:00');

-- Tabela: agendamento_evolucao
INSERT INTO `agendamento_evolucao` (`id`, `evolucao_auditoria_id`, `questao_id`, `resposta`, `data_criacao`) VALUES (50, 40, 58, '<p>Atividade realizada.</p>', '2026-06-10 09:00:00');

-- Tabela: agendamento_evolucao_auditoria
INSERT INTO `agendamento_evolucao_auditoria` (`id`, `formulario_id`, `agendamento_id`, `dt_resposta`, `ativo`, `data_criacao`) VALUES (40, 55, 30, '2026-06-10 09:00:00', 'T', '2026-06-10 09:00:00');

-- Tabela: pacientes
INSERT INTO `pacientes` (`id`, `clinica_id`, `nome`, `dt_nascimento`, `telefone`, `ativo`, `responsavel_nome`, `responsavel_telefone`, `responsavel_email`, `aceita_receber_mensagen_whatsapp`, `data_criacao`) VALUES (10, 13, 'Paciente Sintético', '2020-01-02', '11999999999', 'T', 'Responsável Sintético', '11888888888', NULL, 'T', '2026-01-01 10:00:00');

-- Tabela: procedimento
INSERT INTO `procedimento` (`id`, `clinica_id`, `nome`, `duracao`, `ativo`, `valor`, `tipo`, `valor_total`) VALUES (20, 13, 'Sessão individual', '00:40:00', 'T', 150.00, 'AVULSO', 150.00);

-- Tabela: system_users
INSERT INTO `system_users` (`id`, `email`, `active`, `password`) VALUES (155, 'origem@example.com', 'Y', 'hash-antigo');
"""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_preview_projects_supported_records_without_writing(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    report = await preview_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
    )

    assert report.professional_id == professional.id
    assert report.source_counts == {
        "agendamento": 1,
        "agendamento_evolucao": 1,
        "agendamento_evolucao_auditoria": 1,
        "pacientes": 1,
        "procedimento": 1,
        "system_users": 1,
    }
    assert report.projected_counts == {
        "appointments": 1,
        "caregivers": 1,
        "evolutions": 1,
        "patients": 1,
        "services": 1,
        "sessions": 1,
    }
    assert report.appointment_status_counts == {"concluido": 1}
    assert report.warnings == [
        "O mapeamento dos estados da agenda foi inferido e exige aceite explícito para aplicar."
    ]
    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 0


async def test_apply_requires_sha256_from_reviewed_preview(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    with pytest.raises(
        LegacyClinicImportError,
        match="Informe o SHA-256 exibido no dry-run revisado",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
        )

    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 0


async def test_apply_requires_explicit_acceptance_of_inferred_state_map(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    with pytest.raises(
        LegacyClinicImportError,
        match="Confirme explicitamente o mapeamento inferido dos estados da agenda",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=False,
        )

    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 0


async def test_apply_makes_supported_records_visible_without_financial_side_effects(
    tmp_path, db_session, professional, api_client, auth_headers
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    result = await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )

    assert result.created_counts == {
        "appointments": 1,
        "caregivers": 1,
        "evolutions": 1,
        "patients": 1,
        "services": 1,
        "sessions": 1,
    }
    assert result.verified_counts == {
        "appointments": 1,
        "caregivers": 1,
        "evolutions": 1,
        "patients": 1,
        "services": 1,
        "sessions": 1,
    }
    manifest_path = sql_path.with_name(f"{sql_path.name}.korus-import.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert manifest["sourceFileSha256"] == file_sha256(sql_path)
    assert manifest["professionalId"] == str(professional.id)
    assert manifest["verifiedCounts"] == result.verified_counts
    assert "Paciente Sintético" not in manifest_text
    assert "Responsável Sintético" not in manifest_text
    assert "Atividade realizada" not in manifest_text
    assert "hash-antigo" not in manifest_text

    patients_response = await api_client.get(
        "/api/v1/patients", headers=auth_headers
    )
    assert patients_response.status_code == 200
    imported_patient = patients_response.json()["items"][0]
    assert imported_patient["name"] == "Paciente Sintético"
    assert imported_patient["diagnosisKeys"] == ["nao_informado"]
    patient_id = imported_patient["id"]

    detail_response = await api_client.get(
        f"/api/v1/patients/{patient_id}", headers=auth_headers
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["caregivers"][0] == {
        "id": detail_response.json()["caregivers"][0]["id"],
        "name": "Responsável Sintético",
        "relation": "Responsável",
        "phone": "11888888888",
        "email": "",
        "notes": "",
        "isPrimary": True,
        "whatsappOptIn": True,
    }

    services_response = await api_client.get(
        "/api/v1/finance/services", headers=auth_headers
    )
    assert services_response.status_code == 200
    assert services_response.json()[0]["priceCents"] == 15000

    appointments_response = await api_client.get(
        "/api/v1/appointments?from=2026-06-10&to=2026-06-10",
        headers=auth_headers,
    )
    assert appointments_response.status_code == 200
    assert appointments_response.json()[0]["status"] == "concluido"
    assert appointments_response.json()[0]["servicePriceCents"] == 15000

    sessions_response = await api_client.get(
        f"/api/v1/patients/{patient_id}/sessions", headers=auth_headers
    )
    assert sessions_response.status_code == 200
    assert len(sessions_response.json()) == 1

    evolutions_response = await api_client.get(
        f"/api/v1/patients/{patient_id}/evolutions", headers=auth_headers
    )
    assert evolutions_response.status_code == 200
    assert evolutions_response.json()[0]["content"] == "Atividade realizada."

    finance_response = await api_client.get(
        f"/api/v1/patients/{patient_id}/finance", headers=auth_headers
    )
    assert finance_response.status_code == 200
    assert finance_response.json()["receivables"] == []


async def test_apply_is_idempotent_for_the_same_professional_and_source_ids(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    first = await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )
    second = await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email.upper(),
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )

    assert first.created_counts == {
        "appointments": 1,
        "caregivers": 1,
        "evolutions": 1,
        "patients": 1,
        "services": 1,
        "sessions": 1,
    }
    assert second.created_counts == {
        "appointments": 0,
        "caregivers": 0,
        "evolutions": 0,
        "patients": 0,
        "services": 0,
        "sessions": 0,
    }
    assert second.skipped_counts == {
        "appointments": 1,
        "caregivers": 1,
        "evolutions": 1,
        "patients": 1,
        "services": 1,
        "sessions": 1,
    }


async def test_apply_recovers_manifest_left_pending_after_database_commit(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")
    await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )
    manifest_path = sql_path.with_name(f"{sql_path.name}.korus-import.json")
    pending_path = manifest_path.with_name(f"{manifest_path.name}.pending")
    manifest_path.replace(pending_path)

    recovered = await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )

    assert recovered.created_counts == {
        "appointments": 0,
        "caregivers": 0,
        "evolutions": 0,
        "patients": 0,
        "services": 0,
        "sessions": 0,
    }
    assert manifest_path.exists()
    assert not pending_path.exists()


async def test_apply_blocks_changed_payload_for_an_imported_source_id(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")
    await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )

    changed_sql = LEGACY_SQL.replace("Paciente Sintético", "Paciente Alterado")
    sql_path.write_text(changed_sql, encoding="utf-8")
    with pytest.raises(
        LegacyClinicImportError,
        match="O registro pacientes/10 mudou desde a importação anterior",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
            expected_source_sha256=file_sha256(sql_path),
        )


def test_cli_help_exposes_dry_run_and_explicit_apply_contract():
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "import_legacy_clinic.py"),
            "--help",
        ],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "dry-run" in completed.stdout
    assert "--professional-email" in completed.stdout
    assert "--apply" in completed.stdout
    assert "--accept-inferred-state-map" in completed.stdout
    assert "--manifest" in completed.stdout


def test_importer_requires_no_application_schema_migration():
    assert "legacy_import_records" not in Base.metadata.tables


async def test_apply_blocks_existing_deterministic_rows_without_local_manifest(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")
    await apply_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
        accept_inferred_state_map=True,
        expected_source_sha256=file_sha256(sql_path),
    )
    sql_path.with_name(f"{sql_path.name}.korus-import.json").unlink()

    with pytest.raises(
        LegacyClinicImportError,
        match="existem no destino sem o manifesto local",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
            expected_source_sha256=file_sha256(sql_path),
        )


async def test_apply_blocks_while_automatic_appointment_reminders_are_enabled(
    tmp_path, db_session, professional
):
    db_session.add(
        NotificationSettings(
            professional_id=professional.id,
            whatsapp_enabled=True,
            whatsapp_events={"appointment_reminder_24h": True},
        )
    )
    await db_session.commit()
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    with pytest.raises(
        LegacyClinicImportError,
        match="Desative temporariamente o lembrete automático de 24 horas",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
        )

    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 0


async def test_preview_treats_inactive_source_appointment_as_cancelled(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(
        LEGACY_SQL.replace(
            ", 4, 'S', 'T', NULL, '2026-06-01 10:00:00'",
            ", 1, 'N', 'F', NULL, '2026-06-01 10:00:00'",
        ),
        encoding="utf-8",
    )

    report = await preview_legacy_clinic_import(
        db_session,
        source_path=sql_path,
        professional_email=professional.email,
    )

    assert report.appointment_status_counts == {"cancelado": 1}
    assert report.projected_counts["sessions"] == 0


async def test_apply_blocks_when_file_hash_differs_from_reviewed_preview(
    tmp_path, db_session, professional
):
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    with pytest.raises(
        LegacyClinicImportError,
        match="O SHA-256 do arquivo não corresponde ao dry-run revisado",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
            expected_source_sha256="0" * 64,
        )

    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 0


async def test_apply_blocks_patient_collision_in_target_account(
    tmp_path, db_session, professional
):
    db_session.add(
        Patient(
            professional_id=professional.id,
            name="Paciente Sintético",
            birth_date=date(2020, 1, 2),
            diagnosis_keys=["outros"],
            status="ativo",
            start_date=date(2026, 1, 1),
            avatar_color="oklch(0.58 0.12 205)",
        )
    )
    await db_session.commit()
    sql_path = tmp_path / "legacy.sql"
    sql_path.write_text(LEGACY_SQL, encoding="utf-8")

    with pytest.raises(
        LegacyClinicImportError,
        match="Já existe paciente com o mesmo nome e data de nascimento",
    ):
        await apply_legacy_clinic_import(
            db_session,
            source_path=sql_path,
            professional_email=professional.email,
            accept_inferred_state_map=True,
            expected_source_sha256=file_sha256(sql_path),
        )

    assert await db_session.scalar(select(func.count()).select_from(Patient)) == 1
