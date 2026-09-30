"""Tests for instrument HTTP endpoints."""

import pytest
from httpx import AsyncClient

from app.models.patient import Patient


@pytest.mark.asyncio
async def test_instrument_capabilities(api_client: AsyncClient, auth_headers: dict):
    response = await api_client.get("/api/v1/instruments/fois/capabilities", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["protocolId"] == "fois"
    assert data["scoringMode"] == "manifest"


@pytest.mark.asyncio
async def test_instrument_manifest_and_score(api_client: AsyncClient, auth_headers: dict):
    manifest_resp = await api_client.get("/api/v1/instruments/fois/manifest", headers=auth_headers)
    assert manifest_resp.status_code == 200
    manifest = manifest_resp.json()
    assert manifest["instrumentSlug"] == "fois"

    score_resp = await api_client.post(
        "/api/v1/instruments/fois/score",
        headers=auth_headers,
        json={"answers": {"fois_level": 4}},
    )
    assert score_resp.status_code == 200
    scores = score_resp.json()
    assert scores["total"] == 4


@pytest.mark.asyncio
async def test_create_assessment_with_manifest_scoring(
    api_client: AsyncClient,
    auth_headers: dict,
    patient: Patient,
):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/assessments",
        headers=auth_headers,
        json={
            "protocolId": "fois",
            "answers": {"fois_level": 3},
            "status": "completed",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["protocolId"] == "fois"
    assert body["percentage"] >= 0
    assert body["answers"]["fois_level"] == 3


@pytest.mark.parametrize("finalize_draft", [False, True])
async def test_manifest_persistence_recomputes_forged_scores(
    api_client, auth_headers, patient, db_session, finalize_draft,
):
    from sqlalchemy import select
    from app.models.assessment import Assessment

    base = f"/api/v1/patients/{patient.id}/assessments"
    payload = {"answers": {"fois_level": 1}, "scores": {"total": 7, "summary": "Forjado"},
               "result": "Forjado", "percentage": 100,
               "fields": [{"label": "Forjado", "value": "7"}],
               "interpretation": "Síntese profissional revisada"}
    if finalize_draft:
        draft = await api_client.put(f"{base}/drafts/fois", headers=auth_headers,
                                     json={"answers": {"fois_level": 1}})
        assert draft.status_code == 200
        response = await api_client.post(f"{base}/drafts/fois/complete", headers=auth_headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["id"] == draft.json()["id"]
    else:
        response = await api_client.post(base, headers=auth_headers, json={"protocolId": "fois", **payload})
        assert response.status_code == 201, response.text
    saved = (await db_session.execute(select(Assessment))).scalar_one()
    assert saved.scores["total"] == 1
    assert saved.answers == {"fois_level": 1}
    assert saved.percentage == 14
    assert saved.result != "Forjado"
    assert saved.fields != payload["fields"]
    assert saved.interpretation == "Síntese profissional revisada"
    history = await api_client.get(base, headers=auth_headers)
    assert history.status_code == 200
    assert history.json()[0]["scores"]["total"] == 1


async def test_manifest_cannot_complete_with_scores_and_no_answers(api_client, auth_headers, patient):
    response = await api_client.post(f"/api/v1/patients/{patient.id}/assessments", headers=auth_headers,
                                    json={"protocolId": "fois", "scores": {"total": 7}, "result": "Forjado"})
    assert response.status_code == 400
