"""F16 — corridas reais das respostas públicas (gate PostgreSQL §5.2).

Exigem ``TEST_AUDIT_PG_URL`` apontando para o banco descartável ``korus_audit``
(fixture ``audit_pg_factory``); sem a variável ficam skipped, o que NÃO aprova o
gate — o integrador roda com o banco disponível. SQLite não prova ``FOR UPDATE``
nem ``UNIQUE`` sob concorrência real.

Corridas cobertas: replays concorrentes do mesmo ``clientRecordId``, segunda
resposta concorrente na mesma tarefa e revogação do grant no meio de uma edição
(a revalidação pós-lock impede o commit posterior).
"""

import asyncio
import io
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import func, select, update
from starlette.datastructures import Headers, UploadFile

from app.models.caregiver import Caregiver
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgram,
    HomeProgramCheckIn,
    HomeProgramCheckInRevision,
    HomeProgramEvent,
    HomeProgramGrant,
    HomeProgramPhoto,
    HomeProgramTask,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.storage_cleanup import (
    STORAGE_CLEANUP_PENDING,
    StorageCleanupTask,
)
from app.schemas.home_program import (
    HomeProgramCheckInCreate,
    HomeProgramCheckInUpdate,
)
from app.services import home_program_photo_service, home_program_response_service
from app.utils.token_hash import hash_token


async def _seed_program(factory, *, token: str) -> tuple[UUID, UUID, UUID]:
    async with factory() as db:
        professional = Professional(
            email=f"race-{uuid4().hex}@example.com",
            name="Race",
            password_hash="unused",
        )
        db.add(professional)
        await db.flush()
        patient = Patient(
            professional_id=professional.id,
            name="Synthetic",
            birth_date=date(2020, 1, 1),
            start_date=date.today(),
            avatar_color="teal",
            diagnosis_keys=[],
        )
        db.add(patient)
        await db.flush()
        caregiver = Caregiver(
            patient_id=patient.id, name="Maria", relation="Mãe", is_primary=True
        )
        db.add(caregiver)
        await db.flush()
        goal = Goal(
            patient_id=patient.id,
            professional_id=professional.id,
            title="Meta",
            area="Linguagem",
            start_date=date.today(),
            status="Em andamento",
        )
        db.add(goal)
        await db.flush()
        program = HomeProgram(
            patient_id=patient.id,
            created_by_professional_id=professional.id,
            title="Programa",
            status="active",
            version=2,
            starts_on=date.today() - timedelta(days=1),
            ends_on=date.today() + timedelta(days=13),
            timezone="America/Sao_Paulo",
            published_at=datetime.now(UTC),
        )
        db.add(program)
        await db.flush()
        task = HomeProgramTask(
            program_id=program.id,
            position=0,
            client_task_id=uuid4(),
            title="Tarefa",
            instructions="Instruções",
            due_on=date.today(),
            goal_id=goal.id,
        )
        db.add(task)
        await db.flush()
        grant = HomeProgramGrant(
            program_id=program.id,
            caregiver_id=caregiver.id,
            caregiver_name_snapshot="Maria",
            caregiver_relation_snapshot="Mãe",
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(days=7),
            created_by_professional_id=professional.id,
        )
        db.add(grant)
        await db.commit()
        return program.id, task.id, grant.id


async def test_concurrent_replays_keep_a_single_check_in(audit_pg_factory):
    factory = audit_pg_factory
    token = f"race-{uuid4().hex}"
    _, task_id, _ = await _seed_program(factory, token=token)
    body = HomeProgramCheckInCreate(
        client_record_id=uuid4(), done=True, comment="Feito"
    )

    async def create():
        async with factory() as db:
            result = await home_program_response_service.create_check_in(
                db, raw_token=token, task_id=task_id, body=body
            )
            await db.commit()
            return result

    first, second = await asyncio.wait_for(
        asyncio.gather(create(), create()), timeout=15
    )
    assert first.check_in.id == second.check_in.id
    assert first.check_in.version == second.check_in.version == 1
    assert {first.created, second.created} == {True, False}

    async with factory() as db:
        check_ins = await db.scalar(
            select(func.count()).select_from(HomeProgramCheckIn)
        )
        events = await db.scalar(
            select(func.count()).select_from(HomeProgramEvent)
        )
    assert check_ins == 1
    assert events == 1


async def test_concurrent_second_response_is_rejected(audit_pg_factory):
    factory = audit_pg_factory
    token = f"race-{uuid4().hex}"
    _, task_id, _ = await _seed_program(factory, token=token)

    async def create(client_record_id):
        body = HomeProgramCheckInCreate(
            client_record_id=client_record_id, done=True, comment=None
        )
        async with factory() as db:
            try:
                result = await home_program_response_service.create_check_in(
                    db, raw_token=token, task_id=task_id, body=body
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("created", result.check_in.id)

    outcomes = await asyncio.wait_for(
        asyncio.gather(create(uuid4()), create(uuid4())), timeout=15
    )
    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["created", "rejected"]
    rejected = next(value for kind, value in outcomes if kind == "rejected")
    assert rejected == 409

    async with factory() as db:
        check_ins = await db.scalar(
            select(func.count()).select_from(HomeProgramCheckIn)
        )
        events = await db.scalar(
            select(func.count()).select_from(HomeProgramEvent)
        )
    assert check_ins == 1
    assert events == 1


async def test_revocation_during_update_serializes_with_the_grant(
    audit_pg_factory,
):
    factory = audit_pg_factory
    token = f"race-{uuid4().hex}"
    _, task_id, grant_id = await _seed_program(factory, token=token)

    async with factory() as db:
        created = await home_program_response_service.create_check_in(
            db,
            raw_token=token,
            task_id=task_id,
            body=HomeProgramCheckInCreate(
                client_record_id=uuid4(), done=True, comment="Feito"
            ),
        )
        await db.commit()
    check_in_id = UUID(created.check_in.id)

    body = HomeProgramCheckInUpdate(
        client_record_id=uuid4(),
        expected_version=1,
        done=False,
        comment="Não deu",
    )

    async def update_check_in():
        async with factory() as db:
            try:
                result = await home_program_response_service.update_check_in(
                    db, raw_token=token, check_in_id=check_in_id, body=body
                )
            except HTTPException:
                await db.rollback()
                return None
            await db.commit()
            return result

    async def revoke_grant():
        async with factory() as db:
            await db.execute(
                update(HomeProgramGrant)
                .where(HomeProgramGrant.id == grant_id)
                .values(revoked_at=datetime.now(UTC))
            )
            await db.commit()

    updated, _ = await asyncio.wait_for(
        asyncio.gather(update_check_in(), revoke_grant()), timeout=15
    )

    async with factory() as db:
        grant = await db.get(HomeProgramGrant, grant_id)
        assert grant.revoked_at is not None
        row = await db.get(HomeProgramCheckIn, check_in_id)
        revisions = await db.scalar(
            select(func.count()).select_from(HomeProgramCheckInRevision)
        )
        if updated is None:
            # revogação no intervalo impediu o commit posterior
            assert row.version == 1
            assert revisions == 0
        else:
            assert row.version == 2
            assert revisions == 1
            assert updated.check_in.version == 2


# --------------------------------------------------------------------------- #
# Fotos (Tarefa 5.3): corrida de revogação durante o upload e uploads paralelos
# --------------------------------------------------------------------------- #


def _photo_body() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 36), (120, 30, 200)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _photo_upload() -> UploadFile:
    body = _photo_body()
    return UploadFile(
        file=io.BytesIO(body),
        filename="photo.jpg",
        headers=Headers(raw=[(b"content-type", b"image/jpeg")]),
    )


async def _check_in_for(factory, token: str, task_id: UUID) -> UUID:
    async with factory() as db:
        created = await home_program_response_service.create_check_in(
            db,
            raw_token=token,
            task_id=task_id,
            body=HomeProgramCheckInCreate(
                client_record_id=uuid4(), done=True, comment="Feito"
            ),
        )
        await db.commit()
    return UUID(created.check_in.id)


async def test_revocation_during_photo_upload_blocks_commit_and_queues_cleanup(
    audit_pg_factory, monkeypatch
):
    """Revogação no intervalo do I/O: o vínculo não commita e o blob vai à fila."""
    factory = audit_pg_factory
    token = f"race-{uuid4().hex}"
    _, task_id, grant_id = await _seed_program(factory, token=token)
    check_in_id = await _check_in_for(factory, token, task_id)

    stored: dict[str, bytes] = {}

    async def upload_then_revoke(key: str, body: bytes, content_type: str) -> str:
        stored[key] = body
        # a revogação chega DE FORA enquanto o upload está em andamento
        async with factory() as revoker:
            await revoker.execute(
                update(HomeProgramGrant)
                .where(HomeProgramGrant.id == grant_id)
                .values(revoked_at=datetime.now(UTC))
            )
            await revoker.commit()
        return key

    monkeypatch.setattr(
        "app.services.home_program_photo_service.storage_service.upload",
        upload_then_revoke,
    )

    async with factory() as db:
        with pytest.raises(HTTPException) as excinfo:
            await home_program_photo_service.upload_check_in_photo(
                db,
                raw_token=token,
                check_in_id=check_in_id,
                upload=_photo_upload(),
                client_record_id=uuid4(),
                expected_version=1,
            )
        await db.rollback()
    assert excinfo.value.status_code == 410

    async with factory() as db:
        photos = await db.scalar(
            select(func.count()).select_from(HomeProgramPhoto)
        )
        grant = await db.get(HomeProgramGrant, grant_id)
        row = await db.get(HomeProgramCheckIn, check_in_id)
        pending = (
            (
                await db.execute(
                    select(StorageCleanupTask).where(
                        StorageCleanupTask.status == STORAGE_CLEANUP_PENDING
                    )
                )
            )
            .scalars()
            .all()
        )
    assert photos == 0
    assert grant.revoked_at is not None
    assert row.version == 1 and row.done is True  # check-in textual intacto
    assert len(pending) == 1
    assert stored[pending[0].storage_key]  # blob órfão aguardando o janitor


async def test_concurrent_photo_uploads_keep_a_single_current_photo(
    audit_pg_factory, monkeypatch
):
    """Dois envios concorrentes: um vira a foto vigente, o outro é substituído."""
    factory = audit_pg_factory
    token = f"race-{uuid4().hex}"
    _, task_id, _ = await _seed_program(factory, token=token)
    check_in_id = await _check_in_for(factory, token, task_id)

    stored: dict[str, bytes] = {}

    async def fake_upload(key: str, body: bytes, content_type: str) -> str:
        stored[key] = body
        return key

    monkeypatch.setattr(
        "app.services.home_program_photo_service.storage_service.upload", fake_upload
    )

    async def upload_once():
        async with factory() as db:
            try:
                result = await home_program_photo_service.upload_check_in_photo(
                    db,
                    raw_token=token,
                    check_in_id=check_in_id,
                    upload=_photo_upload(),
                    client_record_id=uuid4(),
                    expected_version=1,
                )
            except HTTPException as exc:
                await db.rollback()
                return ("rejected", exc.status_code)
            await db.commit()
            return ("ok", result.photo.id)

    outcomes = await asyncio.wait_for(
        asyncio.gather(upload_once(), upload_once()), timeout=20
    )
    assert sorted(kind for kind, _ in outcomes) == ["ok", "ok"]

    async with factory() as db:
        photos = (
            (
                await db.execute(
                    select(HomeProgramPhoto).order_by(HomeProgramPhoto.created_at)
                )
            )
            .scalars()
            .all()
        )
        pending = (
            (
                await db.execute(
                    select(StorageCleanupTask).where(
                        StorageCleanupTask.status == STORAGE_CLEANUP_PENDING
                    )
                )
            )
            .scalars()
            .all()
        )
        row = await db.get(HomeProgramCheckIn, check_in_id)
    assert len(photos) == 2
    assert [photo.status for photo in photos].count("ready") == 1
    assert [photo.status for photo in photos].count("deleted") == 1
    assert len(pending) == 1
    assert pending[0].storage_key in stored
    assert row.version == 1 and row.done is True
