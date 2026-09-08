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
