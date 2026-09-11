"""F17/4.2 — corridas reais em PostgreSQL: vínculos e métrica de download.

Sem ``TEST_AUDIT_PG_URL`` estes testes são skipped; eles são o gate de
concorrência real (SQLite em memória não prova UNIQUE nem UPDATE atômico).
"""

import asyncio
import uuid
from datetime import date
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_link import GoalResourceLink
from app.services.resource_link_service import link_goal_resource, unlink_goal_resource
from app.services.resource_service import ResourceService
from app.services.storage import storage_service


async def _seed(factory):
    async with factory() as db:
        owner = Professional(
            email=f"race-{uuid.uuid4().hex[:8]}@example.com",
            name="Race",
            password_hash="unused",
        )
        db.add(owner)
        await db.flush()
        patient = Patient(
            professional_id=owner.id,
            name="Synthetic",
            birth_date=date(2020, 1, 1),
            start_date=date.today(),
            avatar_color="teal",
            diagnosis_keys=[],
        )
        db.add(patient)
        await db.flush()
        goal = Goal(
            patient_id=patient.id,
            professional_id=owner.id,
            title="Meta corrida",
            area="Linguagem",
            progress=10,
            start_date=date.today(),
            status="Inicial",
        )
        resource = Resource(
            owner_professional_id=owner.id,
            title="Material corrida",
            description="",
            categories=[],
            format="PDF",
            file_size_bytes=10,
            author="Race",
            storage_key=f"resources/race/{uuid.uuid4().hex}.pdf",
            content_type="application/pdf",
        )
        db.add_all([goal, resource])
        await db.commit()
        return owner.id, patient.id, goal.id, resource.id


async def test_concurrent_goal_resource_link_keeps_single_pair(audit_pg_factory):
    factory = audit_pg_factory
    owner_id, patient_id, goal_id, resource_id = await _seed(factory)

    async def link():
        async with factory() as db:
            owner = await db.get(Professional, owner_id)
            payload = await link_goal_resource(db, patient_id, goal_id, resource_id, owner)
            await db.commit()
            return payload

    results = await asyncio.wait_for(asyncio.gather(link(), link()), timeout=10)
    assert all(item.available for item in results)
    async with factory() as reader:
        total = (
            await reader.execute(select(func.count()).select_from(GoalResourceLink))
        ).scalar_one()
        assert total == 1, "replay concorrente do mesmo par não duplica vínculo"


async def test_concurrent_unlink_leaves_no_rows(audit_pg_factory):
    factory = audit_pg_factory
    owner_id, patient_id, goal_id, resource_id = await _seed(factory)
    async with factory() as db:
        owner = await db.get(Professional, owner_id)
        await link_goal_resource(db, patient_id, goal_id, resource_id, owner)
        await db.commit()

    async def unlink():
        async with factory() as db:
            owner = await db.get(Professional, owner_id)
            await unlink_goal_resource(db, patient_id, goal_id, resource_id, owner)
            await db.commit()

    await asyncio.wait_for(asyncio.gather(unlink(), unlink()), timeout=10)
    async with factory() as reader:
        total = (
            await reader.execute(select(func.count()).select_from(GoalResourceLink))
        ).scalar_one()
        assert total == 0


async def test_download_url_increments_are_atomic(audit_pg_factory, monkeypatch):
    factory = audit_pg_factory
    owner_id, _patient, _goal, resource_id = await _seed(factory)
    monkeypatch.setattr(
        storage_service,
        "presigned_url",
        AsyncMock(return_value="https://signed.example/file.pdf"),
    )

    async def hit():
        async with factory() as db:
            owner = await db.get(Professional, owner_id)
            await ResourceService(db).download_url(owner, resource_id)
            await db.commit()

    await asyncio.wait_for(asyncio.gather(*[hit() for _ in range(5)]), timeout=10)
    async with factory() as reader:
        resource = await reader.get(Resource, resource_id)
        assert resource.downloads == 5, "UPDATE atômico não perde incrementos concorrentes"
