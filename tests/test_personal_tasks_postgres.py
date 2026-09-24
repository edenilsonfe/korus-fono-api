"""HTTP integration against the disposable PostgreSQL schema from audit_pg_factory."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import app
from app.models.patient import Patient
from app.models.personal_task import PersonalTask
from app.models.professional import Professional


@pytest.mark.asyncio
async def test_personal_tasks_http_persist_scope_and_patient_fk(audit_pg_factory, monkeypatch):
    async with audit_pg_factory() as db:
        owner = Professional(
            email="owner-tasks@example.test",
            password_hash=hash_password("testpass123"),
            name="Dra. Proprietária",
            email_verified_at=datetime.now(UTC),
            trial_ends_at=datetime.now(UTC) + timedelta(days=7),
        )
        other = Professional(
            email="other-tasks@example.test",
            password_hash=hash_password("testpass123"),
            name="Dra. Outra",
            email_verified_at=datetime.now(UTC),
            trial_ends_at=datetime.now(UTC) + timedelta(days=7),
        )
        db.add_all([owner, other])
        await db.flush()
        own_patient = Patient(
            professional_id=owner.id,
            name="Paciente próprio",
            birth_date=date(2020, 1, 1),
            diagnosis_keys=[],
            status="ativo",
            start_date=date(2026, 1, 1),
            avatar_color="oklch(0.58 0.12 205)",
        )
        other_patient = Patient(
            professional_id=other.id,
            name="Paciente de outra conta",
            birth_date=date(2020, 1, 1),
            diagnosis_keys=[],
            status="ativo",
            start_date=date(2026, 1, 1),
            avatar_color="oklch(0.58 0.12 205)",
        )
        db.add_all([own_patient, other_patient])
        await db.commit()

    async def postgres_db():
        async with audit_pg_factory() as db:
            yield db

    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", audit_pg_factory)
    app.dependency_overrides[get_db] = postgres_db
    owner_headers = {"Authorization": f"Bearer {create_access_token(owner.id)}"}
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/v1/personal-tasks",
                headers=owner_headers,
                json={
                    "title": "Preparar devolutiva",
                    "description": "Conferir os resultados",
                    "dueDate": "2026-10-01",
                    "patientId": str(own_patient.id),
                },
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]
            assert created.json()["patientName"] == "Paciente próprio"
            assert created.json()["status"] == "todo"

            assert (await client.get("/api/v1/personal-tasks", headers=other_headers)).json() == []
            assert (
                await client.post(
                    "/api/v1/personal-tasks",
                    headers=owner_headers,
                    json={"title": "Inválida", "patientId": str(other_patient.id)},
                )
            ).status_code == 404
            assert (
                await client.patch(
                    f"/api/v1/personal-tasks/{task_id}",
                    headers=other_headers,
                    json={"status": "done"},
                )
            ).status_code == 404
            assert (
                await client.patch(
                    f"/api/v1/personal-tasks/{task_id}",
                    headers=owner_headers,
                    json={"patientId": str(other_patient.id)},
                )
            ).status_code == 404

            moved = await client.patch(
                f"/api/v1/personal-tasks/{task_id}",
                headers=owner_headers,
                json={"status": "done", "dueDate": None},
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["status"] == "done"
            assert moved.json()["dueDate"] is None

            async with audit_pg_factory() as db:
                saved = await db.scalar(select(PersonalTask).where(PersonalTask.id == task_id))
                assert saved is not None
                assert saved.professional_id == owner.id
                assert saved.patient_id == own_patient.id
                assert saved.status == "done"
                assert saved.due_date is None
                await db.execute(delete(Patient).where(Patient.id == own_patient.id))
                await db.commit()

            listed = await client.get("/api/v1/personal-tasks", headers=owner_headers)
            assert listed.status_code == 200
            assert len(listed.json()) == 1
            assert listed.json()[0]["patientId"] is None
            assert listed.json()[0]["patientName"] is None

            assert (
                await client.delete(f"/api/v1/personal-tasks/{task_id}", headers=other_headers)
            ).status_code == 404
            assert (
                await client.delete(f"/api/v1/personal-tasks/{task_id}", headers=owner_headers)
            ).status_code == 204
            assert (await client.get("/api/v1/personal-tasks", headers=owner_headers)).json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)
