from datetime import UTC, datetime

import pytest

from app.core.security import create_access_token, hash_password
from app.models.professional import Professional


@pytest.mark.asyncio
async def test_personal_task_lifecycle_and_account_isolation(
    api_client, auth_headers, db_session, patient
):
    other = Professional(
        email="other-tasks@example.com",
        password_hash=hash_password("testpass123"),
        name="Outra profissional",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    created = await api_client.post(
        "/api/v1/personal-tasks",
        headers=auth_headers,
        json={
            "title": "  Revisar relatório  ",
            "description": "  Conferir dados  ",
            "dueDate": "2026-10-01",
            "patientId": str(patient.id),
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["title"] == "Revisar relatório"
    assert task["description"] == "Conferir dados"
    assert task["patientName"] == patient.name
    assert task["status"] == "todo"
    assert task["dueDate"] == "2026-10-01"

    listed = await api_client.get("/api/v1/personal-tasks", headers=auth_headers)
    assert [item["id"] for item in listed.json()] == [task["id"]]
    assert (await api_client.get("/api/v1/personal-tasks", headers=other_headers)).json() == []

    changed = await api_client.patch(
        f"/api/v1/personal-tasks/{task['id']}",
        headers=auth_headers,
        json={"status": "doing", "dueDate": None, "patientId": None},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "doing"
    assert changed.json()["dueDate"] is None
    assert changed.json()["patientId"] is None

    assert (
        await api_client.patch(
            f"/api/v1/personal-tasks/{task['id']}",
            headers=other_headers,
            json={"status": "done"},
        )
    ).status_code == 404
    assert (
        await api_client.delete(f"/api/v1/personal-tasks/{task['id']}", headers=other_headers)
    ).status_code == 404
    assert (
        await api_client.post(
            "/api/v1/personal-tasks",
            headers=other_headers,
            json={"title": "Acessar caso", "patientId": str(patient.id)},
        )
    ).status_code == 404
    assert (
        await api_client.patch(
            f"/api/v1/personal-tasks/{task['id']}",
            headers=auth_headers,
            json={"title": "   "},
        )
    ).status_code == 422

    deleted = await api_client.delete(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers)
    assert deleted.status_code == 204
    assert (await api_client.get("/api/v1/personal-tasks", headers=auth_headers)).json() == []
