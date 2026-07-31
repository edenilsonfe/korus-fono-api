"""GET /assessments — filtros de listagem global."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Assessment
from app.models.patient import Patient
from app.models.professional import Professional


@pytest.mark.asyncio
async def test_list_assessments_filters_by_status(
    api_client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
):
    db_session.add_all(
        [
            Assessment(
                patient_id=patient.id,
                professional_id=professional.id,
                protocol_id="fois",
                date=date.today(),
                result="Rascunho",
                percentage=0,
                interpretation="",
                fields=[],
                status="draft",
            ),
            Assessment(
                patient_id=patient.id,
                professional_id=professional.id,
                protocol_id="fois",
                date=date.today(),
                result="Aguardando informante",
                percentage=0,
                interpretation="",
                fields=[],
                status="draft",
            ),
            Assessment(
                patient_id=patient.id,
                professional_id=professional.id,
                protocol_id="fois",
                date=date.today(),
                result="FOIS 5",
                percentage=71,
                interpretation="ok",
                fields=[],
                status="completed",
            ),
            Assessment(
                patient_id=patient.id,
                professional_id=professional.id,
                protocol_id="fois",
                date=date.today(),
                result="Cancelada",
                percentage=0,
                interpretation="",
                fields=[],
                status="cancelled",
            ),
        ]
    )
    await db_session.commit()

    draft = await api_client.get("/api/v1/assessments?status=draft", headers=auth_headers)
    assert draft.status_code == 200
    body = draft.json()
    assert body["total"] >= 1
    assert "statusCounts" in body
    assert body["statusCounts"]["draft"] >= 1
    assert body["statusCounts"]["awaitingInformant"] >= 1
    assert all(item["status"] == "draft" for item in body["items"])
    assert all(
        "aguardando" not in (item.get("result") or "").lower() for item in body["items"]
    )

    awaiting = await api_client.get(
        "/api/v1/assessments?status=awaiting_informant", headers=auth_headers
    )
    assert awaiting.status_code == 200
    assert awaiting.json()["total"] >= 1
    assert all(
        item["status"] == "draft" and "aguardando" in (item.get("result") or "").lower()
        for item in awaiting.json()["items"]
    )

    week = await api_client.get("/api/v1/assessments?period=week", headers=auth_headers)
    assert week.status_code == 200
    assert week.json()["total"] >= 1

    bad = await api_client.get("/api/v1/assessments?status=weird", headers=auth_headers)
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_cancel_assessment_draft(
    api_client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    professional: Professional,
    patient: Patient,
):
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=professional.id,
        protocol_id="fois",
        date=date.today(),
        result="Rascunho",
        percentage=0,
        interpretation="",
        fields=[],
        status="draft",
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)

    resp = await api_client.post(
        f"/api/v1/assessments/{assessment.id}/cancel",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    again = await api_client.post(
        f"/api/v1/assessments/{assessment.id}/cancel",
        headers=auth_headers,
    )
    assert again.status_code == 400
