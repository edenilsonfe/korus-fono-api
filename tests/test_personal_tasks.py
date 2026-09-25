from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token, hash_password
from app.models.professional import Professional
from app.models.personal_task import PersonalTask
from app.models.appointment import Appointment
from app.models.ai import AIReport
from app.core.config import get_settings
from app.services.personal_task_service import _next_due


def test_recurring_dates_keep_original_weekday_and_month_day():
    assert _next_due(date(2026, 9, 21), "weekly", date(2026, 9, 23)) == date(2026, 9, 28)
    assert _next_due(date(2026, 2, 28), "monthly", date(2026, 2, 28), 31) == date(2026, 3, 31)


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


@pytest.mark.asyncio
async def test_custom_columns_order_and_subtasks_are_private_and_persistent(
    api_client, auth_headers, db_session, db_engine, professional
):
    other = Professional(
        email="other-task-columns@example.com",
        password_hash=hash_password("testpass123"),
        name="Outra profissional",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    base = "/api/v1/personal-tasks"

    assert [item["id"] for item in (await api_client.get(f"{base}/columns", headers=auth_headers)).json()] == [
        "todo", "doing", "done",
    ]
    created = await api_client.post(f"{base}/columns", headers=auth_headers, json={"title": "Aguardando retorno"})
    assert created.status_code == 201, created.text
    column_id = created.json()["id"]
    assert (await api_client.post(f"{base}/columns", headers=auth_headers, json={"title": " aguardando retorno "})).status_code == 422
    assert [item["id"] for item in (await api_client.get(f"{base}/columns", headers=other_headers)).json()] == [
        "todo", "doing", "done",
    ]

    task = await api_client.post(base, headers=auth_headers, json={"title": "Pedir documento", "columnKey": column_id})
    assert task.status_code == 201, task.text
    assert task.json()["columnKey"] == column_id
    assert task.json()["status"] == "doing"
    task_id = task.json()["id"]
    subtask = {"id": str(uuid4()), "title": "Conferir autorização", "done": False}
    updated = await api_client.patch(f"{base}/{task_id}", headers=auth_headers, json={"checklist": [subtask]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["checklist"] == [subtask]

    ordered = ["done", column_id, "todo", "doing"]
    reordered = await api_client.put(f"{base}/columns/order", headers=auth_headers, json={"columnIds": ordered})
    assert reordered.status_code == 200, reordered.text
    assert [item["id"] for item in reordered.json()] == ordered
    assert [item["id"] for item in (await api_client.get(f"{base}/columns", headers=auth_headers)).json()] == ordered
    assert (await api_client.put(f"{base}/columns/order", headers=auth_headers, json={"columnIds": ["todo", "todo", "doing", "done"]})).status_code == 422
    assert (await api_client.put(f"{base}/columns/order", headers=other_headers, json={"columnIds": ordered})).status_code == 422
    assert (await api_client.post(base, headers=other_headers, json={"title": "Inválida", "columnKey": column_id})).status_code == 404
    assert (await api_client.post(base, headers=auth_headers, json={
        "title": "Recorrente já concluída", "columnKey": "done",
        "dueDate": "2026-10-01", "repeatRule": "weekly",
    })).status_code == 422
    assert (await api_client.patch(f"{base}/{task_id}", headers=other_headers, json={"columnKey": column_id})).status_code == 404
    assert (await api_client.patch(
        f"{base}/{task_id}", headers=auth_headers,
        json={"columnKey": column_id, "status": "done"},
    )).status_code == 422

    done = await api_client.patch(f"{base}/{task_id}", headers=auth_headers, json={"columnKey": "done"})
    assert done.status_code == 200, done.text
    assert (done.json()["columnKey"], done.json()["status"]) == ("done", "done")
    reopened = await api_client.patch(f"{base}/{task_id}", headers=auth_headers, json={"columnKey": column_id})
    assert (reopened.json()["columnKey"], reopened.json()["status"]) == (column_id, "doing")
    assert (await api_client.get(base, headers=auth_headers)).json()[0]["checklist"] == [subtask]
    async with async_sessionmaker(db_engine)() as fresh:
        saved_board = await fresh.get(Professional, professional.id)
        saved_task = await fresh.get(PersonalTask, UUID(task_id))
        assert [column["id"] for column in saved_board.personal_task_columns] == ordered
        assert saved_task.column_key == column_id
        assert saved_task.checklist == [subtask]


@pytest.mark.asyncio
async def test_task_routine_reminder_and_dashboard_actions(api_client, auth_headers, db_session, patient, professional):
    today = datetime.now(ZoneInfo(get_settings().clinic_timezone)).date()
    appointment = Appointment(
        professional_id=professional.id, patient_id=patient.id, date=today + timedelta(days=1),
        time=time(10, 0), type="Terapia individual", duration=50, status="confirmado",
    )
    old_appointment = Appointment(
        professional_id=professional.id, patient_id=patient.id, date=today - timedelta(days=1),
        time=time(10, 0), type="Terapia individual", duration=50, status="confirmado",
    )
    report = AIReport(
        professional_id=professional.id, patient_id=patient.id, type="evolucao",
        date=today, preview="Rascunho", content="Conferir", status="draft",
    )
    db_session.add_all([appointment, old_appointment, report])
    await db_session.commit()

    no_due = await api_client.post("/api/v1/personal-tasks", headers=auth_headers, json={
        "title": "Rotina sem prazo", "repeatRule": "weekly",
    })
    assert no_due.status_code == 422

    checklist_id = str(uuid4())
    created = await api_client.post("/api/v1/personal-tasks", headers=auth_headers, json={
        "title": "Conferir prontuário", "dueDate": today.isoformat(),
        "appointmentId": str(appointment.id), "repeatRule": "weekly", "remindBeforeDays": 0,
        "checklist": [{"id": checklist_id, "title": "Revisar evolução", "done": False}],
    })
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["patientId"] == str(patient.id)
    assert task["appointmentDate"] == appointment.date.isoformat()
    assert task["checklist"][0]["id"] == checklist_id
    assert (await api_client.post("/api/v1/personal-tasks", headers=auth_headers, json={
        "title": "Checklist inválido", "checklist": [{"id": checklist_id, "title": "  "}],
    })).status_code == 422

    dashboard = await api_client.get("/api/v1/dashboard", headers=auth_headers)
    assert dashboard.status_code == 200, dashboard.text
    actions = dashboard.json()["actionItems"]
    assert {item["kind"] for item in actions} == {"evolution", "report"}
    assert next(item for item in actions if item["kind"] == "evolution")["href"].endswith(
        f"appointmentId={old_appointment.id}"
    )
    assert next(item for item in actions if item["kind"] == "report")["href"].endswith(
        f"reportId={report.id}"
    )

    notifications = await api_client.get("/api/v1/notifications", headers=auth_headers)
    assert notifications.status_code == 200, notifications.text
    notices = [item for item in notifications.json()["items"] if item["type"] == "task_due"]
    assert len(notices) == 1
    assert notices[0]["deepLink"] == f"/tarefas?taskId={task['id']}"

    checked = await api_client.patch(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers, json={
        "checklist": [{"id": checklist_id, "title": "Revisar evolução", "done": True}],
    })
    assert checked.status_code == 200, checked.text
    assert checked.json()["checklist"][0]["done"] is True
    completed = await api_client.patch(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers, json={"columnKey": "done"})
    assert completed.status_code == 200, completed.text
    listed = (await api_client.get("/api/v1/personal-tasks", headers=auth_headers)).json()
    next_task = next(item for item in listed if item["status"] == "todo")
    assert next_task["columnKey"] == "todo"
    assert next_task["dueDate"] == (today + timedelta(days=7)).isoformat()
    assert next_task["patientId"] == str(patient.id)
    assert next_task["appointmentId"] is None
    assert next_task["checklist"][0]["done"] is False
    assert not [item for item in (await api_client.get("/api/v1/notifications", headers=auth_headers)).json()["items"] if item["type"] == "task_due"]

    # Completing the same card again must not create another occurrence.
    await api_client.patch(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers, json={"status": "done"})
    assert len((await api_client.get("/api/v1/personal-tasks", headers=auth_headers)).json()) == 2
    await api_client.patch(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers, json={"status": "doing"})
    await api_client.patch(f"/api/v1/personal-tasks/{task['id']}", headers=auth_headers, json={"status": "done"})
    assert len((await api_client.get("/api/v1/personal-tasks", headers=auth_headers)).json()) == 2
