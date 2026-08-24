"""Evolution title is optional across validation, persistence, and timeline emission."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.evolution import Evolution
from app.models.patient import Patient
from app.models.timeline import TimelineEvent


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_create_evolution_without_title(
    api_client: AsyncClient,
    auth_headers: dict,
    patient: Patient,
    db_session: AsyncSession,
):
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=auth_headers,
        json={"content": "Paciente participou das atividades propostas."},
    )

    assert response.status_code == 201, response.text
    assert response.json()["title"] is None

    evolution = (
        await db_session.execute(select(Evolution).where(Evolution.patient_id == patient.id))
    ).scalar_one()
    assert evolution.title is None

    timeline_event = (
        await db_session.execute(
            select(TimelineEvent).where(TimelineEvent.source_id == evolution.id)
        )
    ).scalar_one()
    assert timeline_event.title == "Evolução registrada"

