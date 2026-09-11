"""F3 — GET /patients/{id}/report-sources: owner-scoped selectable sources."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.security import create_access_token, hash_password
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session

LLM_DRAFT = "## Síntese\nSíntese do laudo.\n\n## Conduta\nConduta sugerida."


def _assessment_body(protocol_id: str, day: str, percentage: int = 50):
    return {
        "protocolId": protocol_id,
        "date": day,
        "result": "Resultado",
        "percentage": percentage,
        "scores": {"domains": {"linguagem": 5}, "total": 10},
        "answers": {"1": "sim"},
    }


async def _create_assessment(api_client, auth_headers, patient, body):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_battery_aggregate(db_session, patient, professional):
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=professional.id,
        protocol_id="abfw",
        date=date(2026, 4, 1),
        result="Bateria aplicada",
        percentage=72,
        interpretation="",
        fields=[],
        answers={},
        scores={"engine": "battery_module", "total": 72},
        status="completed",
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


async def _add_evolution(db_session, patient, professional, *, content, day=(2026, 3, 2)):
    evolution = Evolution(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime(*day, 14, 0, tzinfo=UTC),
        title="Sessão",
        content=content,
    )
    db_session.add(evolution)
    await db_session.commit()
    await db_session.refresh(evolution)
    return evolution


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value=LLM_DRAFT)
    monkeypatch.setattr("app.services.report_composition_service.run_llm", mock)
    return mock


async def test_report_sources_lists_completed_assessments_with_battery_as_one_item(
    api_client, auth_headers, db_session, professional, patient
):
    await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    battery = await _add_battery_aggregate(db_session, patient, professional)
    draft = await api_client.put(
        f"/api/v1/patients/{patient.id}/assessments/drafts/mchat",
        headers=auth_headers,
        json={"answers": {"1": "sim"}},
    )
    assert draft.status_code == 200

    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "assessment"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert body["limit"] == 20
    assert body["total"] == 2
    items = body["items"]
    assert len(items) == 2
    assert {item["status"] for item in items} == {"completed"}
    assert str(draft.json()["id"]) not in {item["id"] for item in items}

    battery_item = next(item for item in items if item["id"] == str(battery.id))
    assert battery_item["kind"] == "assessment"
    assert battery_item["label"] == "Teste de Linguagem Infantil ABFW"
    assert battery_item["protocolId"] == "abfw"
    assert battery_item["date"] == "2026-04-01"
    assert battery_item["authorName"] == professional.name
    assert battery_item["summary"]

    portage_item = next(item for item in items if item["id"] != str(battery.id))
    assert portage_item["label"].startswith("Inventário Portage")
    assert portage_item["protocolId"] == "portage"
    assert len(portage_item["summary"]) <= 200


async def test_report_sources_lists_evolutions_sessions_and_goals(
    api_client, auth_headers, db_session, professional, patient
):
    evolution = await _add_evolution(db_session, patient, professional, content="Conteúdo da evolução")
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime(2026, 3, 3, 15, 0, tzinfo=UTC),
        duration=50,
        type="Terapia fonoaudiológica",
        objectives=["Ampliar vocabulário"],
        notes="Notas da sessão",
    )
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Ampliar vocabulário",
        area="Linguagem",
        progress=45,
        start_date=date(2026, 2, 1),
        status="Em andamento",
    )
    db_session.add_all([session, goal])
    await db_session.commit()
    await db_session.refresh(session)
    await db_session.refresh(goal)

    evolutions = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "evolution"},
    )
    assert evolutions.status_code == 200, evolutions.text
    item = evolutions.json()["items"][0]
    assert item["id"] == str(evolution.id)
    assert item["kind"] == "evolution"
    assert item["label"] == "Sessão"
    assert item["date"] == "2026-03-02"
    assert item["protocolId"] is None
    assert item["authorName"] == professional.name
    assert "Conteúdo da evolução" in item["summary"]

    sessions = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "session"},
    )
    assert sessions.status_code == 200, sessions.text
    session_item = sessions.json()["items"][0]
    assert session_item["id"] == str(session.id)
    assert session_item["label"] == "Terapia fonoaudiológica"
    assert session_item["date"] == "2026-03-03"
    assert "Notas da sessão" in session_item["summary"]

    goals = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "goal"},
    )
    assert goals.status_code == 200, goals.text
    goal_item = goals.json()["items"][0]
    assert goal_item["id"] == str(goal.id)
    assert goal_item["label"] == "Ampliar vocabulário"
    assert goal_item["date"] == "2026-02-01"
    assert "45" in goal_item["summary"]


async def test_report_sources_pagination(api_client, auth_headers, patient):
    for day, percentage in (
        ("2026-01-10", 20),
        ("2026-02-10", 30),
        ("2026-03-10", 40),
    ):
        await _create_assessment(
            api_client, auth_headers, patient, _assessment_body("portage", day, percentage)
        )

    first = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "assessment", "limit": 2},
    )
    assert first.status_code == 200
    assert first.json()["total"] == 3
    assert [item["date"] for item in first.json()["items"]] == ["2026-03-10", "2026-02-10"]

    second = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "assessment", "limit": 2, "page": 2},
    )
    assert second.status_code == 200
    assert [item["date"] for item in second.json()["items"]] == ["2026-01-10"]


async def test_report_sources_rejects_invalid_kind_window_and_pagination(
    api_client, auth_headers, patient
):
    base_url = f"/api/v1/patients/{patient.id}/report-sources"
    assert (await api_client.get(base_url, headers=auth_headers)).status_code == 422
    invalid_kind = await api_client.get(
        base_url, headers=auth_headers, params={"kind": "prescription"}
    )
    assert invalid_kind.status_code == 422
    assert "Tipo" in invalid_kind.json()["detail"] or "inválido" in invalid_kind.json()["detail"]

    inverted = await api_client.get(
        base_url,
        headers=auth_headers,
        params={"kind": "assessment", "from": "2026-06-01", "to": "2026-01-01"},
    )
    assert inverted.status_code == 422

    bad_page = await api_client.get(
        base_url, headers=auth_headers, params={"kind": "assessment", "page": 0}
    )
    assert bad_page.status_code == 422
    big_limit = await api_client.get(
        base_url, headers=auth_headers, params={"kind": "assessment", "limit": 101}
    )
    assert big_limit.status_code == 422


async def test_report_sources_date_window_filters_assessments(
    api_client, auth_headers, patient
):
    await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-06-10", 60)
    )

    windowed = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "assessment", "from": "2026-05-01", "to": "2026-06-30"},
    )
    assert windowed.status_code == 200, windowed.text
    assert windowed.json()["total"] == 1
    assert windowed.json()["items"][0]["protocolId"] == "vanderbilt"


async def test_report_sources_are_isolated_between_professionals(
    api_client, auth_headers, db_session, patient
):
    other = Professional(
        email="other-sources@example.com",
        password_hash=hash_password("testpass123"),
        name="Dr. Outro",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999990004",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.flush()
    foreign_patient = Patient(
        professional_id=other.id,
        name="Paciente Alheio",
        birth_date=date(2021, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(foreign_patient)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    denied = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=headers,
        params={"kind": "assessment"},
    )
    assert denied.status_code == 404

    # The shared owner of the other patient sees only their own (empty) listing.
    empty = await api_client.get(
        f"/api/v1/patients/{foreign_patient.id}/report-sources",
        headers=headers,
        params={"kind": "assessment"},
    )
    assert empty.status_code == 200
    assert empty.json()["total"] == 0


async def test_report_sources_summary_is_trimmed_but_selection_uses_full_text(
    api_client, auth_headers, db_session, professional, patient, llm
):
    long_text = "A" * 1000
    evolution = await _add_evolution(db_session, patient, professional, content=long_text)
    assessment = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )

    listing = await api_client.get(
        f"/api/v1/patients/{patient.id}/report-sources",
        headers=auth_headers,
        params={"kind": "evolution"},
    )
    item = listing.json()["items"][0]
    assert item["id"] == str(evolution.id)
    assert len(item["summary"]) <= 200
    assert item["summary"] != long_text

    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    created = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "consolidado",
            "composition": {
                "assessmentIds": [str(assessment["id"]), str(other["id"])],
                "evolutionIds": [str(evolution.id)],
            },
        },
    )
    assert created.status_code == 201, created.text
    prompt = llm.await_args.args[0]
    assert long_text in prompt
