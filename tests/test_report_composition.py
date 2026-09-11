"""F3 — consolidated multi-instrument report: selection, versions and composition."""

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.ai import AIJob, AIReport
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_composition import AIReportComposition
from app.models.session import Session

LLM_DRAFT = (
    "## Síntese\nO paciente demonstra avanço nas habilidades avaliadas, com dados "
    "consolidados de mais de um instrumento.\n\n"
    "## Conduta\nManter terapia semanal e reavaliar em três meses."
)


def _assessment_body(protocol_id: str, day: str, percentage: int = 50, result: str = "Atraso leve"):
    return {
        "protocolId": protocol_id,
        "date": day,
        "result": result,
        "percentage": percentage,
        "informant": "Mãe",
        "scores": {
            "domains": {"linguagem": percentage // 10, "motor": 5},
            "total": 15,
            "summary": "Resumo sintético",
        },
        "answers": {"1": "sim", "2": "nao"},
    }


async def _create_assessment(api_client, auth_headers, patient, body):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_draft_assessment(api_client, auth_headers, patient, protocol_id: str):
    response = await api_client.put(
        f"/api/v1/patients/{patient.id}/assessments/drafts/{protocol_id}",
        headers=auth_headers,
        json={"answers": {"1": "sim"}},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _add_evolution(db_session, patient, professional, *, content: str, day=(2026, 3, 2)):
    evolution = Evolution(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime(*day, 14, 0, tzinfo=UTC),
        title="Sessão de terapia",
        content=content,
    )
    db_session.add(evolution)
    await db_session.commit()
    await db_session.refresh(evolution)
    return evolution


async def _add_session(db_session, patient, professional, *, notes: str = "Notas da sessão"):
    session = Session(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime(2026, 3, 2, 15, 0, tzinfo=UTC),
        duration=50,
        type="Terapia fonoaudiológica",
        objectives=["Ampliar vocabulário"],
        notes=notes,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _add_goal(db_session, patient, professional, *, title: str = "Ampliar vocabulário"):
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title=title,
        area="Linguagem",
        progress=45,
        start_date=date(2026, 2, 1),
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _add_battery_aggregate(db_session, patient, professional):
    """Battery result lives in the aggregated Assessment row (one item)."""
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=professional.id,
        protocol_id="abfw",
        date=date(2026, 4, 1),
        result="Bateria aplicada",
        percentage=72,
        interpretation="",
        fields=[],
        answers={"fonologia": "parcial"},
        scores={
            "engine": "battery_module",
            "domains": {"fonologia": {"title": "Fonologia", "score": 80}},
            "total": 72,
            "norms_status": {
                "level": "partial",
                "label": "Normas BR parciais",
                "detail": "Parte das faixas etárias possui tabelas oficiais.",
            },
        },
        status="completed",
        informant=None,
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


@pytest.fixture
def llm(monkeypatch):
    mock = AsyncMock(return_value=LLM_DRAFT)
    monkeypatch.setattr("app.services.report_composition_service.run_llm", mock)
    return mock


async def _create_consolidated(api_client, auth_headers, patient, composition):
    return await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "consolidado",
            "composition": composition,
        },
    )


async def _selection_scaffold(api_client, auth_headers, db_session, professional, patient):
    """Two Portage (base/target), one Vanderbilt, one battery, one of each extra source."""
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    target = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-06-10", 68)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    battery = await _add_battery_aggregate(db_session, patient, professional)
    evolution = await _add_evolution(db_session, patient, professional, content="Evolução selecionada completa.")
    session = await _add_session(db_session, patient, professional)
    goal = await _add_goal(db_session, patient, professional)
    return {
        "base": base,
        "target": target,
        "other": other,
        "battery": battery,
        "evolution": evolution,
        "session": session,
        "goal": goal,
    }


async def test_create_consolidated_report_captures_selected_sources_only(
    api_client, auth_headers, db_session, professional, patient, llm, monkeypatch
):
    # The generic ai_context builder must not be a fallback for consolidado.
    def _forbidden_builder(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("build_context não deve ser usado no laudo consolidado")

    monkeypatch.setattr("app.api.v1.ai.build_context", _forbidden_builder)

    scaffold = await _selection_scaffold(api_client, auth_headers, db_session, professional, patient)
    excluded = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("cars", "2026-02-02", 30)
    )

    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [
                str(scaffold["base"]["id"]),
                str(scaffold["target"]["id"]),
                str(scaffold["other"]["id"]),
                str(scaffold["battery"].id),
            ],
            "evolutionIds": [str(scaffold["evolution"].id)],
            "sessionIds": [str(scaffold["session"].id)],
            "goalIds": [str(scaffold["goal"].id)],
            "comparisons": [
                {"baseId": str(scaffold["target"]["id"]), "targetId": str(scaffold["base"]["id"])}
            ],
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["status"] == "draft"
    assert data["version"] == 1
    assert data["compositionId"]

    content = data["content"]
    headings = ["## Identificação", "## Instrumentos", "## Síntese", "## Conduta"]
    positions = [content.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "João Silva" in content
    assert LLM_DRAFT.splitlines()[1] in content

    # A battery enters once, as the aggregated Assessment item.
    assert content.count("ABFW") == 1
    assert "Portage" in content and "Vanderbilt" in content
    assert "CARS" not in content

    # Only selected sources reach the LLM prompt, with full permitted content.
    llm.assert_awaited_once()
    prompt = llm.await_args.args[0]
    assert "Evolução selecionada completa." in prompt
    assert "Comparações" in prompt
    assert "Normas BR parciais" in prompt
    assert "CARS" not in prompt
    assert "cars" not in prompt

    # Composition endpoint returns restricted provenance only.
    composition = await api_client.get(
        f"/api/v1/ai/reports/{data['id']}/composition", headers=auth_headers
    )
    assert composition.status_code == 200, composition.text
    body = composition.json()
    assert body["reportId"] == data["id"]
    assert body["id"] == data["compositionId"]
    assert body["templateVersion"]
    assert len(body["contextHash"]) == 64
    assert body["capturedAt"] is not None
    sources = body["sources"]
    assert len(sources) == 7
    kinds = {(item["kind"], item["id"]) for item in sources}
    assert ("assessment", str(scaffold["battery"].id)) in kinds
    assert ("evolution", str(scaffold["evolution"].id)) in kinds
    assert ("session", str(scaffold["session"].id)) in kinds
    assert ("goal", str(scaffold["goal"].id)) in kinds
    assert all(item["sourceHash"] and len(item["sourceHash"]) == 64 for item in sources)
    assert str(excluded["id"]) not in json.dumps(body)
    assert "answers" not in json.dumps(body)
    comparisons = body["comparisons"]
    assert len(comparisons) == 1
    assert comparisons[0]["baseId"] == str(scaffold["base"]["id"])
    assert comparisons[0]["targetId"] == str(scaffold["target"]["id"])
    assert comparisons[0]["percentageDelta"] == 28
    assert any("Normas" in warning for warning in body["warnings"])

    # List keeps the legacy payload plus version/compositionId.
    listing = await api_client.get("/api/v1/ai/reports", headers=auth_headers)
    row = next(item for item in listing.json() if item["id"] == data["id"])
    assert row["version"] == 1
    assert row["compositionId"] == data["compositionId"]
    assert row["content"] == ""

    detail = await api_client.get(f"/api/v1/ai/reports/{data['id']}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["version"] == 1
    assert detail.json()["compositionId"] == data["compositionId"]

    stored = await db_session.scalar(
        select(AIReportComposition).where(AIReportComposition.report_id == UUID(data["id"]))
    )
    assert stored is not None
    assert stored.context_hash == body["contextHash"]
    assert stored.supersedes_report_id is None
    assert str(stored.professional_id) == str(professional.id)
    job = await db_session.scalar(
        select(AIJob).where(AIJob.job_type == "report", AIJob.patient_id == patient.id)
    )
    assert job is not None and job.status == "completed"


async def test_legacy_reports_keep_working_without_composition(
    api_client, auth_headers, db_session, professional, patient, llm
):
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="clinico",
        date=date(2026, 9, 1),
        preview="Legado",
        content="## Identificação\nTexto legado",
        status="draft",
    )
    db_session.add(report)
    await db_session.commit()

    detail = await api_client.get(f"/api/v1/ai/reports/{report.id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["version"] is not None
    assert detail.json()["compositionId"] is None

    missing = await api_client.get(
        f"/api/v1/ai/reports/{report.id}/composition", headers=auth_headers
    )
    assert missing.status_code == 404

    patch = await api_client.patch(
        f"/api/v1/ai/reports/{report.id}",
        headers=auth_headers,
        json={"content": "## Identificação\nTexto revisado", "status": "finalized"},
    )
    assert patch.status_code == 200
    assert patch.json()["compositionId"] is None
    revisions = await api_client.get(
        f"/api/v1/ai/reports/{report.id}/revisions", headers=auth_headers
    )
    assert revisions.status_code == 200


async def test_consolidated_requires_composition_and_forbids_it_for_other_types(
    api_client, auth_headers, patient, llm
):
    missing = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={"patientId": str(patient.id), "type": "consolidado"},
    )
    assert missing.status_code == 422

    forbidden = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "clinico",
            "composition": {
                "assessmentIds": [str(uuid4()), str(uuid4())],
            },
        },
    )
    assert forbidden.status_code == 422


async def test_consolidated_rejects_unknown_composition_fields(
    api_client, auth_headers, patient, llm
):
    response = await api_client.post(
        "/api/v1/ai/reports",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "type": "consolidado",
            "composition": {
                "assessmentIds": [str(uuid4()), str(uuid4())],
                "unknownField": True,
            },
        },
    )
    assert response.status_code == 422


async def test_consolidated_rejects_duplicate_ids(
    api_client, auth_headers, db_session, professional, patient, llm
):
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    evolution = await _add_evolution(db_session, patient, professional, content="Texto")

    duplicated_assessments = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [str(base["id"]), str(base["id"]), str(other["id"])],
        },
    )
    assert duplicated_assessments.status_code == 422
    assert "duplicad" in duplicated_assessments.json()["detail"].lower()

    duplicated_evolution = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [str(base["id"]), str(other["id"])],
            "evolutionIds": [str(evolution.id), str(evolution.id)],
        },
    )
    assert duplicated_evolution.status_code == 422
    assert "duplicad" in duplicated_evolution.json()["detail"].lower()

    assert llm.await_count == 0


async def test_consolidated_requires_two_distinct_protocols(
    api_client, auth_headers, patient, llm
):
    first = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    second = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-06-10", 68)
    )
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(first["id"]), str(second["id"])]},
    )
    assert response.status_code == 422
    assert "protocolos" in response.json()["detail"]
    assert llm.await_count == 0


async def test_consolidated_rejects_assessment_that_is_not_completed(
    api_client, auth_headers, patient, llm
):
    done = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    draft = await _create_draft_assessment(api_client, auth_headers, patient, "vanderbilt")
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(done["id"]), str(draft["id"])]},
    )
    assert response.status_code == 409
    assert "concluídas" in response.json()["detail"]
    assert llm.await_count == 0


async def test_consolidated_rejects_out_of_scope_sources(
    api_client, auth_headers, db_session, professional, patient, llm
):
    first = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    second = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    valid = [str(first["id"]), str(second["id"])]

    missing_assessment = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(first["id"]), str(uuid4())]},
    )
    assert missing_assessment.status_code == 404

    missing_evolution = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": valid, "evolutionIds": [str(uuid4())]},
    )
    assert missing_evolution.status_code == 404

    missing_session = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": valid, "sessionIds": [str(uuid4())]},
    )
    assert missing_session.status_code == 404

    missing_goal = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": valid, "goalIds": [str(uuid4())]},
    )
    assert missing_goal.status_code == 404

    # Foreign professional/patient pair must never be touched.
    other = Professional(
        email="other-composition@example.com",
        password_hash=hash_password("testpass123"),
        name="Dr. Outro",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11999990003",
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
    await db_session.flush()
    foreign_assessment = Assessment(
        patient_id=foreign_patient.id,
        professional_id=other.id,
        protocol_id="portage",
        date=date(2026, 1, 1),
        result="Alheio",
        percentage=10,
        interpretation="",
        fields=[],
        answers={},
        scores=None,
        status="completed",
    )
    db_session.add(foreign_assessment)
    await db_session.commit()

    foreign_ids = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(first["id"]), str(foreign_assessment.id)]},
    )
    assert foreign_ids.status_code == 404

    foreign_patient_response = await _create_consolidated(
        api_client,
        auth_headers,
        foreign_patient,
        {"assessmentIds": [str(uuid4()), str(uuid4())]},
    )
    assert foreign_patient_response.status_code == 404

    supersedes = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": valid, "supersedesReportId": str(uuid4())},
    )
    assert supersedes.status_code == 404
    assert llm.await_count == 0


async def test_consolidated_rejects_invalid_comparisons(
    api_client, auth_headers, patient, llm
):
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    selected = [str(base["id"]), str(other["id"])]

    cross_protocol = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": selected,
            "comparisons": [{"baseId": str(base["id"]), "targetId": str(other["id"])}],
        },
    )
    assert cross_protocol.status_code == 422
    assert "mesmo protocolo" in cross_protocol.json()["detail"]

    not_selected = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": selected,
            "comparisons": [{"baseId": str(base["id"]), "targetId": str(uuid4())}],
        },
    )
    assert not_selected.status_code == 422
    assert "selecionad" in not_selected.json()["detail"].lower()

    same_assessment = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": selected,
            "comparisons": [{"baseId": str(base["id"]), "targetId": str(base["id"])}],
        },
    )
    assert same_assessment.status_code == 422

    duplicated_pair = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": selected,
            "comparisons": [
                {"baseId": str(base["id"]), "targetId": str(other["id"])},
                {"baseId": str(other["id"]), "targetId": str(base["id"])},
            ],
        },
    )
    assert duplicated_pair.status_code == 422
    assert llm.await_count == 0


async def test_consolidated_budget_excess_returns_422_without_truncation(
    api_client, auth_headers, db_session, professional, patient, llm, monkeypatch
):
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    await _add_evolution(db_session, patient, professional, content="x" * 4000)
    monkeypatch.setattr(get_settings(), "ai_context_max_chars", 500)

    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [str(base["id"]), str(other["id"])],
        },
    )
    assert response.status_code == 422
    assert "orçamento" in response.json()["detail"].lower()
    assert llm.await_count == 0
    assert await db_session.scalar(select(func.count(AIReport.id))) == 0


async def test_consolidated_provider_failure_returns_503_without_empty_draft(
    api_client, auth_headers, db_session, patient, llm
):
    llm.side_effect = HTTPException(
        status_code=503,
        detail="Serviço de IA temporariamente indisponível. Tente novamente em alguns minutos.",
        headers={"Retry-After": "60"},
    )
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(base["id"]), str(other["id"])]},
    )
    assert response.status_code == 503
    assert response.headers.get("retry-after") == "60"
    assert await db_session.scalar(select(func.count(AIReport.id))) == 0
    assert llm.await_count == 1


async def test_consolidated_empty_provider_output_is_not_a_successful_draft(
    api_client, auth_headers, db_session, patient, llm
):
    llm.return_value = ""
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(base["id"]), str(other["id"])]},
    )
    assert response.status_code == 503
    assert await db_session.scalar(select(func.count(AIReport.id))) == 0


async def test_consolidated_detects_source_change_during_generation(
    api_client, auth_headers, db_session, patient, llm
):
    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )

    async def mutate_then_draft(*_args, **_kwargs):
        assessment = await db_session.get(Assessment, UUID(base["id"]))
        assessment.result = "Resultado alterado durante a geração"
        await db_session.flush()
        return LLM_DRAFT

    llm.side_effect = mutate_then_draft
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {"assessmentIds": [str(base["id"]), str(other["id"])]},
    )
    assert response.status_code == 409
    assert "mudaram" in response.json()["detail"]
    assert llm.await_count == 1  # never retried automatically
    assert await db_session.scalar(select(func.count(AIReport.id))) == 0
    assert await db_session.scalar(select(func.count(AIReportComposition.id))) == 0


async def test_consolidated_supersedes_report_in_scope(
    api_client, auth_headers, db_session, professional, patient, llm
):
    previous = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="consolidado",
        date=date(2026, 8, 1),
        preview="Anterior",
        content="## Identificação\nAnterior",
        status="draft",
    )
    db_session.add(previous)
    await db_session.commit()

    base = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("portage", "2026-01-10", 40)
    )
    other = await _create_assessment(
        api_client, auth_headers, patient, _assessment_body("vanderbilt", "2026-02-01", 55)
    )
    response = await _create_consolidated(
        api_client,
        auth_headers,
        patient,
        {
            "assessmentIds": [str(base["id"]), str(other["id"])],
            "supersedesReportId": str(previous.id),
        },
    )
    assert response.status_code == 201, response.text
    composition = await api_client.get(
        f"/api/v1/ai/reports/{response.json()['id']}/composition", headers=auth_headers
    )
    assert composition.status_code == 200
    assert composition.json()["supersedesReportId"] == str(previous.id)
