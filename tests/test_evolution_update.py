from uuid import UUID

from sqlalchemy import select

from app.models.evolution import Evolution
from app.models.patient import Patient
from app.models.timeline import TimelineEvent


async def test_update_evolution_updates_record_and_timeline(
    api_client, auth_headers, patient: Patient, db_session
):
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/evolutions",
        headers=auth_headers,
        json={"title": "Antes", "content": "Conteúdo original"},
    )
    evolution_id = created.json()["id"]

    response = await api_client.patch(
        f"/api/v1/patients/{patient.id}/evolutions/{evolution_id}",
        headers=auth_headers,
        json={"title": "Depois", "content": "  Conteúdo atualizado  "},
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Depois"
    assert response.json()["content"] == "Conteúdo atualizado"

    evolution = await db_session.get(Evolution, UUID(evolution_id))
    timeline = await db_session.scalar(
        select(TimelineEvent).where(TimelineEvent.source_id == evolution.id)
    )
    assert evolution.title == timeline.title == "Depois"
    assert evolution.content == timeline.description == "Conteúdo atualizado"
