"""DELETE /patients/{id} — remove patient and cascaded clinical data."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.goal import Goal
from app.models.home_program import HomeProgram, HomeProgramGrant, HomeProgramTask
from app.models.patient import Patient
from app.models.patient_record_export import PatientRecordExport
from app.models.professional import Professional
from app.models.report_delivery import ReportDelivery
from app.utils.token_hash import hash_token


@pytest.mark.asyncio
async def test_delete_patient_returns_204(api_client, db_session, professional, monkeypatch):
    monkeypatch.setattr("app.api.v1.auth.enforce_login_rate_limit", lambda *_a, **_k: None)
    patient = Patient(
        professional_id=professional.id,
        name="Para excluir",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)

    login = await api_client.post(
        "/api/v1/auth/login",
        json={"email": professional.email, "password": "testpass123"},
    )
    assert login.status_code == 200
    assert "korus_access" in login.cookies

    deleted = await api_client.delete(f"/api/v1/patients/{patient.id}")
    assert deleted.status_code == 204

    missing = await api_client.get(f"/api/v1/patients/{patient.id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_delete_patient_other_professional_returns_404(api_client, db_session, professional, monkeypatch):
    monkeypatch.setattr("app.api.v1.auth.enforce_login_rate_limit", lambda *_a, **_k: None)
    other = Professional(
        email=f"other-{uuid4().hex[:8]}@test.com",
        password_hash=hash_password("testpass123"),
        name="Outra profissional",
        specialty_key="fono",
        specialty="Fonoaudiologia",
    )
    db_session.add(other)
    await db_session.flush()
    foreign = Patient(
        professional_id=other.id,
        name="Paciente alheio",
        birth_date=date(2019, 5, 5),
        diagnosis_keys=[],
        status="avaliacao",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign)
    await db_session.commit()

    login = await api_client.post(
        "/api/v1/auth/login",
        json={"email": professional.email, "password": "testpass123"},
    )
    assert login.status_code == 200
    assert "korus_access" in login.cookies

    response = await api_client.delete(f"/api/v1/patients/{foreign.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_caregiver_revokes_derived_school_deliveries(
    api_client, auth_headers, db_session, professional, patient, monkeypatch
):
    """F20: deleting the authorizing caregiver revokes the school deliveries it
    authorized, before the row disappears; the historical reference stays."""
    monkeypatch.setattr(
        "app.services.school_report_delivery_service.send_email",
        MagicMock(return_value="email-msg-1"),
    )
    primary = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    other = Caregiver(
        patient_id=patient.id, name="Outro responsável", relation="Pai", is_primary=False
    )
    db_session.add(other)
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="escolar",
        date=date(2026, 9, 1),
        preview="Relatório escolar",
        content="## Síntese\nTexto escolar revisado.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(other)
    await db_session.refresh(report)

    def _school_body(caregiver_id):
        return {
            "channel": "email",
            "recipientKind": "school",
            "email": "escola@example.com",
            "school": {"name": "Escola Municipal Vila Nova", "recipientName": "Coordenação"},
            "schoolAuthorization": {
                "caregiverId": str(caregiver_id),
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "evidenceReference": "Termo assinado arquivado no prontuário",
                "reviewed": True,
            },
        }

    first = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json=_school_body(primary.id),
    )
    assert first.status_code == 201, first.text
    second = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json=_school_body(other.id),
    )
    assert second.status_code == 201, second.text
    standard = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert standard.status_code == 201, standard.text

    first_token = first.json()["url"].rsplit("/", 1)[-1]
    second_token = second.json()["url"].rsplit("/", 1)[-1]
    standard_token = standard.json()["url"].rsplit("/", 1)[-1]
    assert (await api_client.get(f"/api/v1/report-deliveries/{first_token}")).status_code == 200

    deleted = await api_client.delete(
        f"/api/v1/patients/{patient.id}/caregivers/{primary.id}", headers=auth_headers
    )
    assert deleted.status_code == 204

    # Only the delivery authorized by the deleted caregiver is revoked.
    assert (await api_client.get(f"/api/v1/report-deliveries/{first_token}")).status_code == 410
    assert (await api_client.get(f"/api/v1/report-deliveries/{second_token}")).status_code == 200
    assert (await api_client.get(f"/api/v1/report-deliveries/{standard_token}")).status_code == 200

    revoked = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(first.json()["id"]))
    )
    await db_session.refresh(revoked)
    assert revoked.revoked_at is not None
    assert revoked.school_authorization["caregiverId"] == str(primary.id)
    assert revoked.school_authorization["evidenceReference"] == (
        "Termo assinado arquivado no prontuário"
    )

    kept = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(second.json()["id"]))
    )
    await db_session.refresh(kept)
    assert kept.revoked_at is None


@pytest.mark.asyncio
async def test_delete_patient_with_export_history_returns_409_and_suggests_inactivation(
    api_client, auth_headers, db_session, patient, professional
):
    """F6: histórico de exportação impede a exclusão física; a resposta aponta
    a inativação em vez de estourar FK/500 — e nada é removido."""
    db_session.add(
        PatientRecordExport(
            patient_id=patient.id,
            professional_id=professional.id,
            kind="summary",
            format="pdf",
            sections=["identification", "goals", "sessions"],
            purpose="care_continuity",
            status="generated",
            requested_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            record_counts={"goals": 0, "sessions": 0},
            attachment_count=0,
            size_bytes=1024,
            sha256="c" * 64,
        )
    )
    await db_session.commit()

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}", headers=auth_headers
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "exportação" in detail
    assert "inativo" in detail

    kept = await db_session.get(Patient, patient.id)
    assert kept is not None
    exports = (
        await db_session.execute(
            select(PatientRecordExport).where(
                PatientRecordExport.patient_id == patient.id
            )
        )
    ).scalars().all()
    assert len(exports) == 1


async def _active_home_program_with_grant(db_session, patient, professional):
    """Programa de casa ativo + grant para o responsável principal."""
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Meta de casa",
        area="Linguagem",
        start_date=date.today(),
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.flush()
    program = HomeProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Programa de casa",
        status="active",
        version=2,
        starts_on=date.today(),
        ends_on=date.today() + timedelta(days=10),
        timezone="America/Sao_Paulo",
    )
    db_session.add(program)
    await db_session.flush()
    db_session.add(
        HomeProgramTask(
            program_id=program.id,
            position=0,
            client_task_id=uuid4(),
            title="Tarefa",
            instructions="Instruções da tarefa",
            due_on=date.today(),
            goal_id=goal.id,
        )
    )
    grant = HomeProgramGrant(
        program_id=program.id,
        caregiver_id=caregiver.id,
        caregiver_name_snapshot=caregiver.name,
        caregiver_relation_snapshot=caregiver.relation,
        token_hash=hash_token(f"token-{uuid4().hex}"),
        expires_at=datetime.now(UTC) + timedelta(days=5),
        created_by_professional_id=professional.id,
        family_authorization={
            "authorizedAt": datetime.now(UTC).isoformat(),
            "reviewed": True,
        },
    )
    db_session.add(grant)
    await db_session.commit()
    await db_session.refresh(grant)
    return caregiver, grant


@pytest.mark.asyncio
async def test_delete_caregiver_revokes_home_program_grants(
    api_client, auth_headers, db_session, patient, professional
):
    """F16: excluir o responsável revoga seus grants antes de removê-lo; o
    snapshot de autoria permanece e o vínculo vira nulo (FK SET NULL)."""
    caregiver, grant = await _active_home_program_with_grant(
        db_session, patient, professional
    )

    deleted = await api_client.delete(
        f"/api/v1/patients/{patient.id}/caregivers/{caregiver.id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204

    await db_session.refresh(grant)
    assert grant.revoked_at is not None
    assert grant.caregiver_id is None
    assert grant.caregiver_name_snapshot == "Maria Silva"
    assert grant.caregiver_relation_snapshot == "Mãe"


@pytest.mark.asyncio
async def test_delete_patient_with_home_program_history_returns_409(
    api_client, auth_headers, db_session, patient, professional
):
    """F16: histórico de programa de casa impede a exclusão física; a resposta
    aponta a inativação e nada é removido."""
    db_session.add(
        HomeProgram(
            patient_id=patient.id,
            created_by_professional_id=professional.id,
            title="Programa de casa",
            status="draft",
            version=1,
            starts_on=date.today(),
            ends_on=date.today() + timedelta(days=5),
            timezone="America/Sao_Paulo",
        )
    )
    await db_session.commit()

    response = await api_client.delete(
        f"/api/v1/patients/{patient.id}", headers=auth_headers
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "programa de casa" in detail
    assert "inativo" in detail

    kept = await db_session.get(Patient, patient.id)
    assert kept is not None
    programs = (
        await db_session.execute(
            select(HomeProgram).where(HomeProgram.patient_id == patient.id)
        )
    ).scalars().all()
    assert len(programs) == 1
