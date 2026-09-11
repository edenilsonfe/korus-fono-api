"""F16 fase 3 — foto opcional da resposta familiar (Tarefa 5.3).

Cobre o contrato do plano §3.5: foto JPEG/PNG até 5 MiB e 12 megapixels com
DECODIFICAÇÃO REAL e remoção de EXIF/GPS/texto (provada lendo o blob de volta
com Pillow), uma foto vigente por resposta (a nova substitui a anterior na
mesma transação de vínculo), comandos idempotentes por ``clientRecordId`` e
controle otimista pela versão do check-in, reserva de limpeza ANTES do I/O com
revalidação do grant na finalização (revogação no intervalo impede o commit e
agenda limpeza), falha de foto deixando o check-in textual INTACTO, leitura
pública (grant) e privada (ACL clínica), teto de 10 uploads/10 min por grant e
erros 410/413/422/404/403/503 do contrato. SQLite em memória; Pillow real;
storage sempre stub local. A corrida real fica em
``tests/test_home_program_concurrency.py`` (gate PostgreSQL).
"""

import hashlib
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import func, select, update

from app.core.security import create_access_token, hash_password
from app.models.care_team import (
    PatientCareTeamMember,
)
from app.models.caregiver import Caregiver
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgramCheckIn,
    HomeProgramEvent,
    HomeProgramGrant,
    HomeProgramPhoto,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.storage_cleanup import (
    STORAGE_CLEANUP_PENDING,
    STORAGE_CLEANUP_RESOLVED,
    StorageCleanupTask,
)
from app.services import storage_cleanup_service
from app.services.home_program_photo_service import (
    MAX_PHOTO_BYTES,
    MAX_PHOTO_PIXELS,
    process_photo_bytes,
)

TODAY = date.today()
TOKEN_HEADER = "X-Home-Program-Token"
PUBLIC = "/api/v1/home-program-responses"
MAX_PHOTO_SIZE_LABEL = "5 MiB"


def _headers(token: str | None) -> dict[str, str]:
    return {} if token is None else {TOKEN_HEADER: token}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Imagens reais (Pillow) — metadados de verdade para provar o strip
# --------------------------------------------------------------------------- #


def jpeg_bytes(*, size: tuple[int, int] = (64, 48), color=(180, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def jpeg_with_gps_bytes(*, size: tuple[int, int] = (72, 54)) -> bytes:
    image = Image.new("RGB", size, (40, 120, 60))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    exif[0x0110] = "Model X"
    exif[0x8825] = {1: "S", 2: 23.55, 3: "W", 4: 46.6333}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def png_with_text_bytes(*, size: tuple[int, int] = (48, 32)) -> bytes:
    image = Image.new("RGBA", size, (20, 90, 160, 200))
    info = PngInfo()
    info.add_text("camera", "MADE-BY-CAMERA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def _stored_image(body: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(body))
    image.load()
    return image


class FakeMissingKeyError(FileNotFoundError):
    """Objeto ausente no storage falso (mesma semântica do S3 NoSuchKey)."""


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: o contador público é sempre 'permite'.

    Os testes de limite específicos re-patcham ``_redis_allow`` no próprio corpo
    (negação/queda do contador); aqui garante-se que a suíte roda sem docker.
    """
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


@pytest.fixture
def fake_storage(monkeypatch):
    """Storage em memória compartilhado por serviço de foto e janitor."""
    objects: dict[str, bytes] = {}
    content_types: dict[str, str] = {}
    uploads: list[str] = []
    deletes: list[str] = []

    async def fake_upload(key: str, body: bytes, content_type: str) -> str:
        uploads.append(key)
        objects[key] = body
        content_types[key] = content_type
        return key

    async def fake_download_limited(
        key: str, max_bytes: int, timeout_seconds: float = 30.0
    ) -> tuple[bytes, str | None]:
        if key not in objects:
            raise FakeMissingKeyError(key)
        return objects[key], content_types.get(key)

    async def fake_delete(key: str) -> None:
        deletes.append(key)
        objects.pop(key, None)

    monkeypatch.setattr(
        "app.services.home_program_photo_service.storage_service.upload", fake_upload
    )
    monkeypatch.setattr(
        "app.services.home_program_photo_service.storage_service.download_limited",
        fake_download_limited,
    )
    monkeypatch.setattr(
        "app.services.storage_cleanup_service.storage_service.delete", fake_delete
    )
    return SimpleNamespace(
        objects=objects,
        content_types=content_types,
        uploads=uploads,
        deletes=deletes,
        download_limited=fake_download_limited,
    )


# --------------------------------------------------------------------------- #
# Fixtures de programa/grant/check-in (mesmos padrões da Tarefa 5.2)
# --------------------------------------------------------------------------- #


def _task(
    *,
    goal_id: UUID | None = None,
    aba_id: UUID | None = None,
    resource_ids: list[UUID] | None = None,
    **overrides,
) -> dict:
    task = {
        "title": "Nomear figuras",
        "instructions": "Mostre os cartões e peça o nome.",
        "dueOn": TODAY.isoformat(),
        "goalId": str(goal_id) if goal_id else None,
        "interventionProgramId": str(aba_id) if aba_id else None,
        "resourceIds": [str(resource_id) for resource_id in (resource_ids or [])],
    }
    task.update(overrides)
    return task


async def _goal(db_session, patient: Patient, professional: Professional) -> Goal:
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title="Nomear animais",
        area="Linguagem",
        start_date=TODAY,
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _published_program(
    api_client,
    auth_headers,
    patient: Patient,
    db_session,
    professional: Professional,
    *,
    task_count: int = 1,
) -> dict:
    goal = await _goal(db_session, patient, professional)
    body = {
        "title": "Rotina da casa",
        "startsOn": TODAY.isoformat(),
        "endsOn": (TODAY + timedelta(days=13)).isoformat(),
        "tasks": [_task(goal_id=goal.id) for _ in range(task_count)],
    }
    created = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=body,
    )
    assert created.status_code == 201, created.text
    data = created.json()
    published = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{data['id']}/publish",
        headers=auth_headers,
        json={"expectedVersion": 1},
    )
    assert published.status_code == 200, published.text
    return published.json()


async def _issue_grant(
    api_client, auth_headers, db_session, patient: Patient, program: dict
) -> tuple[str, HomeProgramGrant]:
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants",
        headers=auth_headers,
        json={
            "caregiverId": str(caregiver.id),
            "familyAuthorization": {
                "authorizedAt": (
                    datetime.now(UTC) - timedelta(days=1)
                ).isoformat(),
                "reference": "Termo de autorização arquivado",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    token = data["url"].split("#token=", 1)[1]
    grant = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == UUID(data["id"]))
    )
    await db_session.refresh(grant)
    return token, grant


async def _check_in(
    api_client, token: str, task_id: str, *, done: bool = True, comment=None
) -> dict:
    response = await api_client.post(
        f"{PUBLIC}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid.uuid4()),
            "done": done,
            "comment": comment,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _photo_env(
    api_client, auth_headers, db_session, patient, professional
) -> SimpleNamespace:
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    check_in = await _check_in(api_client, token, program["tasks"][0]["id"])
    return SimpleNamespace(
        program=program,
        token=token,
        grant=grant,
        check_in=check_in,
        check_in_id=UUID(check_in["id"]),
    )


async def _upload(
    api_client,
    token: str | None,
    check_in_id: UUID,
    body: bytes,
    *,
    client_record_id: UUID | None = None,
    expected_version: int = 1,
    content_type: str = "image/jpeg",
    filename: str = "foto.jpg",
):
    return await api_client.put(
        f"{PUBLIC}/check-ins/{check_in_id}/photo",
        headers=_headers(token),
        data={
            "clientRecordId": str(client_record_id or uuid.uuid4()),
            "expectedVersion": str(expected_version),
        },
        files={"file": (filename, body, content_type)},
    )


async def _delete_photo(
    api_client,
    token: str | None,
    check_in_id: UUID,
    *,
    client_record_id: UUID | None = None,
    expected_version: int = 1,
):
    return await api_client.request(
        "DELETE",
        f"{PUBLIC}/check-ins/{check_in_id}/photo",
        headers=_headers(token),
        json={
            "clientRecordId": str(client_record_id or uuid.uuid4()),
            "expectedVersion": expected_version,
        },
    )


async def _enable_aba_flag(db_session) -> None:
    existing = await db_session.get(FeatureFlag, "multidisciplinary_aba")
    if existing is None:
        db_session.add(
            FeatureFlag(
                key="multidisciplinary_aba",
                description="Equipe multiprofissional e programas ABA",
                enabled_global=True,
            )
        )
    else:
        existing.enabled_global = True
    await db_session.commit()


async def _consent(
    api_client, auth_headers, patient: Patient, decision: str = "granted"
) -> dict:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": decision, "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _other_professional(
    db_session, *, email: str = "outra@example.com", name: str = "Dra. Outra"
) -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=name,
        specialty_key="fono",
        specialty="Fonoaudiologia",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


def _professional_headers(professional: Professional) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_access_token(professional.id, professional.token_version)}"
        )
    }


# --------------------------------------------------------------------------- #
# Validação real da imagem: EXIF/GPS/texto e limites
# --------------------------------------------------------------------------- #


def test_photo_processor_strips_metadata_and_enforces_limits():
    raw = jpeg_with_gps_bytes()
    processed = process_photo_bytes(raw, "image/jpeg")
    assert processed.content_type == "image/jpeg"
    assert processed.body != raw  # reencodada, não é o arquivo original
    stored = _stored_image(processed.body)
    assert stored.format == "JPEG"
    assert stored.size == (72, 54)
    assert dict(stored.getexif()) == {}
    assert stored.getexif().get_ifd(0x8825) == {}

    png = process_photo_bytes(png_with_text_bytes(), "image/png")
    assert png.content_type == "image/png"
    assert b"MADE-BY-CAMERA" not in png.body
    stored_png = _stored_image(png.body)
    assert stored_png.format == "PNG"
    assert getattr(stored_png, "text", {}) == {}

    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(b"<svg xmlns=...></svg>", "image/svg+xml")
    assert getattr(excinfo.value, "status_code", None) == 422
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp")
    assert getattr(excinfo.value, "status_code", None) == 422
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(jpeg_bytes(), "video/mp4")
    assert getattr(excinfo.value, "status_code", None) == 422
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(jpeg_bytes(), "image/png")  # magic divergente
    assert getattr(excinfo.value, "status_code", None) == 422
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(b"\xff\xd8\xffgarbage", "image/jpeg")
    assert getattr(excinfo.value, "status_code", None) == 422
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(b"x" * (MAX_PHOTO_BYTES + 1), "image/jpeg")
    assert getattr(excinfo.value, "status_code", None) == 413
    with pytest.raises(Exception) as excinfo:
        process_photo_bytes(jpeg_bytes(size=(4200, 3000)), "image/jpeg")
    assert getattr(excinfo.value, "status_code", None) == 422
    assert 4200 * 3000 > MAX_PHOTO_PIXELS


async def test_upload_strips_exif_gps_and_reencodes_jpeg(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    raw = jpeg_with_gps_bytes()

    response = await _upload(api_client, env.token, env.check_in_id, raw)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"id", "hasPhoto", "version"}
    assert data["hasPhoto"] is True
    assert data["version"] == 1

    assert len(fake_storage.uploads) == 1
    key = fake_storage.uploads[0]
    assert key == (
        f"patients/{patient.id}/home-programs/{env.program['id']}/photos/"
        f"{data['id']}/photo.jpg"
    )
    stored = fake_storage.objects[key]
    assert stored != raw
    image = _stored_image(stored)
    assert image.format == "JPEG"
    assert image.size == (72, 54)
    assert dict(image.getexif()) == {}
    assert image.getexif().get_ifd(0x8825) == {}

    photo = await db_session.scalar(
        select(HomeProgramPhoto).where(HomeProgramPhoto.id == UUID(data["id"]))
    )
    await db_session.refresh(photo)
    assert photo.status == "ready"
    assert photo.storage_key == key
    assert photo.content_type == "image/jpeg"
    assert photo.size_bytes == len(stored)
    assert photo.sha256 == hashlib.sha256(stored).hexdigest()

    # foto NUNCA é Recurso
    count = await db_session.scalar(select(func.count()).select_from(Resource))
    assert count == 0


async def test_upload_accepts_png_and_drops_text_chunks(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    raw = png_with_text_bytes()
    assert b"MADE-BY-CAMERA" in raw

    response = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        raw,
        content_type="image/png",
        filename="foto.png",
    )
    assert response.status_code == 200, response.text
    key = fake_storage.uploads[0]
    assert key.endswith("/photo.png")
    stored = fake_storage.objects[key]
    assert b"MADE-BY-CAMERA" not in stored
    image = _stored_image(stored)
    assert image.format == "PNG"
    assert image.size == (48, 32)
    assert getattr(image, "text", {}) == {}


async def test_photo_is_independent_from_marking_done(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    check_in = await _check_in(
        api_client, token, program["tasks"][0]["id"], done=False, comment="Ainda não"
    )
    check_in_id = UUID(check_in["id"])

    # não exige done=true
    uploaded = await _upload(api_client, token, check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200, uploaded.text

    # a foto não muda a versão nem o texto da resposta
    public = (await api_client.get(PUBLIC, headers=_headers(token))).json()
    embedded = public["tasks"][0]["checkIn"]
    assert embedded["version"] == 1
    assert embedded["done"] is False
    assert embedded["comment"] == "Ainda não"
    assert embedded["hasPhoto"] is True

    # o PATCH textual segue funcionando com a mesma versão
    patched = await api_client.patch(
        f"{PUBLIC}/check-ins/{check_in_id}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid.uuid4()),
            "expectedVersion": 1,
            "done": True,
            "comment": "Fizemos!",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2
    assert patched.json()["hasPhoto"] is True


async def test_upload_rejects_limits_types_and_corruption(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)

    oversized = await _upload(
        api_client, env.token, env.check_in_id, b"x" * (MAX_PHOTO_BYTES + 1)
    )
    assert oversized.status_code == 413, oversized.text
    assert MAX_PHOTO_SIZE_LABEL in oversized.json()["detail"]

    svg = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        content_type="image/svg+xml",
    )
    assert svg.status_code == 422, svg.text
    webp = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        content_type="image/webp",
    )
    assert webp.status_code == 422
    video = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        jpeg_bytes(),
        content_type="video/mp4",
    )
    assert video.status_code == 422
    mismatched = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        jpeg_bytes(),
        content_type="image/png",
    )
    assert mismatched.status_code == 422
    corrupt = await _upload(
        api_client, env.token, env.check_in_id, b"\xff\xd8\xffgarbage"
    )
    assert corrupt.status_code == 422, corrupt.text
    huge = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        jpeg_bytes(size=(4200, 3000)),
    )
    assert huge.status_code == 422, huge.text

    # nada foi enviado nem reservado; o check-in segue intacto
    assert fake_storage.uploads == []
    photos = await db_session.scalar(
        select(func.count()).select_from(HomeProgramPhoto)
    )
    pending = await db_session.scalar(
        select(func.count())
        .select_from(StorageCleanupTask)
        .where(StorageCleanupTask.status == STORAGE_CLEANUP_PENDING)
    )
    assert photos == 0 and pending == 0
    row = await db_session.scalar(
        select(HomeProgramCheckIn).where(HomeProgramCheckIn.id == env.check_in_id)
    )
    await db_session.refresh(row)
    assert row.version == 1 and row.done is True


async def test_upload_reserves_before_io_and_resolves_on_association(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    order: list[str] = []
    reservation_seen: list[str] = []

    import app.services.home_program_photo_service as photo_service_module

    real_reserve = photo_service_module.reserve_storage_cleanup
    fixture_upload = photo_service_module.storage_service.upload

    async def spy_reserve(db, storage_key, *, reason, professional_id=None):
        order.append("reserve")
        task = await real_reserve(
            db, storage_key, reason=reason, professional_id=professional_id
        )
        reservation_seen.append(task.status)
        return task

    async def spy_upload(key, body, content_type):
        order.append("upload")
        return await fixture_upload(key, body, content_type)

    monkeypatch.setattr(photo_service_module, "reserve_storage_cleanup", spy_reserve)
    monkeypatch.setattr(
        photo_service_module.storage_service, "upload", spy_upload
    )

    response = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert response.status_code == 200, response.text
    assert order == ["reserve", "upload"]
    assert reservation_seen == [STORAGE_CLEANUP_PENDING]

    reservations = (
        (
            await db_session.execute(
                select(StorageCleanupTask).order_by(StorageCleanupTask.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(reservations) == 1
    assert reservations[0].status == STORAGE_CLEANUP_RESOLVED
    assert reservations[0].storage_key in fake_storage.objects


async def test_new_photo_replaces_current_and_cleans_old_blob(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    first = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert first.status_code == 200, first.text
    first_id = UUID(first.json()["id"])
    first_key = fake_storage.uploads[0]

    second = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        jpeg_bytes(color=(10, 200, 10)),
        expected_version=1,
    )
    assert second.status_code == 200, second.text
    second_id = UUID(second.json()["id"])
    assert second_id != first_id
    assert len(fake_storage.uploads) == 2

    rows = (
        (
            await db_session.execute(
                select(HomeProgramPhoto).order_by(HomeProgramPhoto.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in rows] == ["deleted", "ready"]
    assert rows[0].id == first_id and rows[1].id == second_id
    assert rows[0].storage_key == first_key  # referência histórica preservada

    public = (await api_client.get(PUBLIC, headers=_headers(env.token))).json()
    assert public["tasks"][0]["checkIn"]["hasPhoto"] is True

    # o blob antigo é removido pelo janitor; a foto vigente permanece
    summary = await storage_cleanup_service.run_storage_cleanup(db_session)
    assert summary["deleted"] == 1
    assert fake_storage.deletes == [first_key]
    assert first_key not in fake_storage.objects
    assert rows[1].storage_key in fake_storage.objects


async def test_upload_failure_keeps_check_in_intact_and_blob_reserved(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)

    import app.services.home_program_photo_service as photo_service_module

    async def broken_upload(key, body, content_type):
        raise RuntimeError("storage fora do ar")

    monkeypatch.setattr(
        photo_service_module.storage_service, "upload", broken_upload
    )

    response = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert response.status_code == 503, response.text
    assert "indisponível" in response.json()["detail"]

    # check-in textual intacto e sem foto vinculada
    row = await db_session.scalar(
        select(HomeProgramCheckIn).where(HomeProgramCheckIn.id == env.check_in_id)
    )
    await db_session.refresh(row)
    assert row.version == 1 and row.done is True
    photos = await db_session.scalar(
        select(func.count()).select_from(HomeProgramPhoto)
    )
    assert photos == 0

    # a reserva sobrevive para o janitor limpar o órfão (se algo chegou ao S3)
    pending = (
        (
            await db_session.execute(
                select(StorageCleanupTask).where(
                    StorageCleanupTask.status == STORAGE_CLEANUP_PENDING
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1
    fake_storage.objects[pending[0].storage_key] = b"orphan"
    summary = await storage_cleanup_service.run_storage_cleanup(db_session)
    assert summary["deleted"] == 1
    assert pending[0].storage_key not in fake_storage.objects


async def test_revocation_during_upload_blocks_commit_and_queues_cleanup(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)

    import app.services.home_program_photo_service as photo_service_module

    async def revoking_upload(key, body, content_type):
        # revogação externa chega no meio do I/O de upload
        await db_session.execute(
            update(HomeProgramGrant)
            .where(HomeProgramGrant.id == env.grant.id)
            .values(revoked_at=datetime.now(UTC))
        )
        await db_session.commit()
        fake_storage.objects[key] = body
        fake_storage.uploads.append(key)
        return key

    monkeypatch.setattr(
        photo_service_module.storage_service, "upload", revoking_upload
    )

    response = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert response.status_code == 410, response.text

    # revogação no intervalo: nada de foto, check-in intacto, blob na fila
    photos = await db_session.scalar(
        select(func.count()).select_from(HomeProgramPhoto)
    )
    assert photos == 0
    row = await db_session.scalar(
        select(HomeProgramCheckIn).where(HomeProgramCheckIn.id == env.check_in_id)
    )
    await db_session.refresh(row)
    assert row.version == 1 and row.done is True
    grant = await db_session.scalar(
        select(HomeProgramGrant).where(HomeProgramGrant.id == env.grant.id)
    )
    await db_session.refresh(grant)
    assert grant.revoked_at is not None

    pending = (
        (
            await db_session.execute(
                select(StorageCleanupTask).where(
                    StorageCleanupTask.status == STORAGE_CLEANUP_PENDING
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1
    assert pending[0].storage_key in fake_storage.objects

    summary = await storage_cleanup_service.run_storage_cleanup(db_session)
    assert summary["deleted"] == 1
    assert pending[0].storage_key not in fake_storage.objects


# --------------------------------------------------------------------------- #
# Idempotência, versão e escopo do comando
# --------------------------------------------------------------------------- #


async def test_upload_version_conflict_replay_and_record_reuse(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    record_id = uuid.uuid4()
    raw = jpeg_bytes()

    stale = await _upload(
        api_client, env.token, env.check_in_id, raw, expected_version=7
    )
    assert stale.status_code == 409, stale.text
    assert fake_storage.uploads == []

    created = await _upload(
        api_client, env.token, env.check_in_id, raw, client_record_id=record_id
    )
    assert created.status_code == 200, created.text
    photo_id = created.json()["id"]

    replay = await _upload(
        api_client, env.token, env.check_in_id, raw, client_record_id=record_id
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == photo_id
    assert len(fake_storage.uploads) == 1  # replay não reenvia o blob

    reused = await _upload(
        api_client,
        env.token,
        env.check_in_id,
        jpeg_bytes(color=(1, 2, 3)),
        client_record_id=record_id,
    )
    assert reused.status_code == 409, reused.text
    assert "identificador" in reused.json()["detail"]

    events = await db_session.scalar(
        select(func.count())
        .select_from(HomeProgramEvent)
        .where(HomeProgramEvent.event_type == "home_program_photo_uploaded")
    )
    assert events == 1


async def test_upload_requires_grant_and_scopes_to_the_check_in(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    program_a = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token_a, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program_a
    )
    check_in_a = await _check_in(api_client, token_a, program_a["tasks"][0]["id"])

    # sem token → 410 genérico, sem I/O
    anonymous = await _upload(api_client, None, UUID(check_in_a["id"]), jpeg_bytes())
    assert anonymous.status_code == 410
    assert fake_storage.uploads == []

    # rotação revoga o grant anterior: o token antigo não escreve mais
    token_b, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program_a
    )
    rotated = await _upload(api_client, token_a, UUID(check_in_a["id"]), jpeg_bytes())
    assert rotated.status_code == 410
    assert fake_storage.uploads == []

    # check-in fora do programa do grant → 404
    foreign = await _upload(api_client, token_b, uuid.uuid4(), jpeg_bytes())
    assert foreign.status_code == 404
    assert fake_storage.uploads == []


async def test_multipart_payload_validation(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)

    missing_fields = await api_client.put(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo",
        headers=_headers(env.token),
        files={"file": ("foto.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert missing_fields.status_code == 422

    zero_version = await api_client.put(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo",
        headers=_headers(env.token),
        data={"clientRecordId": str(uuid.uuid4()), "expectedVersion": "0"},
        files={"file": ("foto.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert zero_version.status_code == 422

    bad_record = await api_client.put(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo",
        headers=_headers(env.token),
        data={"clientRecordId": "not-a-uuid", "expectedVersion": "1"},
        files={"file": ("foto.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert bad_record.status_code == 422
    assert fake_storage.uploads == []


async def test_read_only_owner_blocks_photo_writes_but_allows_reads(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200

    professional.subscription_status = "canceled"
    await db_session.commit()

    blocked_write = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert blocked_write.status_code == 403, blocked_write.text
    assert "indisponíve" in blocked_write.json()["detail"]
    assert "assinatura" not in blocked_write.json()["detail"].lower()

    blocked_delete = await _delete_photo(api_client, env.token, env.check_in_id)
    assert blocked_delete.status_code == 403

    # leitura da foto continua em read-only
    read = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert read.status_code == 200, read.text
    assert read.content == fake_storage.objects[
        fake_storage.uploads[0]
    ]


# --------------------------------------------------------------------------- #
# Leitura dos bytes: pública (grant) e privada (ACL clínica)
# --------------------------------------------------------------------------- #


async def test_public_photo_file_serves_bytes_with_contract_headers(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200

    response = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == fake_storage.objects[fake_storage.uploads[0]]
    assert _stored_image(response.content).size == (64, 48)


async def test_public_photo_file_scope_and_lifecycle_errors(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200
    key = fake_storage.uploads[0]

    # check-in fora do grant → 404
    outside = await api_client.get(
        f"{PUBLIC}/check-ins/{uuid.uuid4()}/photo/file",
        headers=_headers(env.token),
    )
    assert outside.status_code == 404

    # sem token → 410
    anonymous = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file"
    )
    assert anonymous.status_code == 410

    # blob ausente no storage → 404 neutro
    fake_storage.objects.pop(key)
    missing = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert missing.status_code == 404

    # storage fora do ar → 503
    import app.services.home_program_photo_service as photo_service_module

    async def broken_download(*args, **kwargs):
        raise RuntimeError("storage fora do ar")

    monkeypatch.setattr(
        photo_service_module.storage_service, "download_limited", broken_download
    )
    unavailable = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert unavailable.status_code == 503

    # restaura o stub e remove a foto: o arquivo deixa de existir (404)
    monkeypatch.setattr(
        photo_service_module.storage_service,
        "download_limited",
        fake_storage.download_limited,
    )
    removed = await _delete_photo(api_client, env.token, env.check_in_id)
    assert removed.status_code == 200
    gone = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert gone.status_code == 404


async def test_public_photo_file_410_after_grant_revoked(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200

    revoke = await api_client.delete(
        f"/api/v1/patients/{patient.id}/home-programs/{env.program['id']}/grants/"
        f"{env.grant.id}",
        headers=auth_headers,
    )
    assert revoke.status_code == 204, revoke.text

    response = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert response.status_code == 410
    assert "inválido ou indisponível" in response.json()["detail"]


async def test_professional_tracking_and_photo_acl(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200

    base = (
        f"/api/v1/patients/{patient.id}/home-programs/{env.program['id']}/check-ins"
    )
    owner_headers = _professional_headers(professional)
    listing = await api_client.get(base, headers=owner_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == {
        "id",
        "taskId",
        "taskTitle",
        "done",
        "comment",
        "respondedAt",
        "version",
        "hasPhoto",
        "actorLabel",
    }
    assert item["taskTitle"] == "Nomear figuras"
    assert item["hasPhoto"] is True
    assert item["actorLabel"] == "Maria Silva (Mãe)"  # dono vê o responsável
    assert item["id"] == env.check_in["id"]

    owner_photo = await api_client.get(
        base + f"/{env.check_in_id}/photo/file", headers=owner_headers
    )
    assert owner_photo.status_code == 200, owner_photo.text
    assert owner_photo.content == fake_storage.objects[fake_storage.uploads[0]]

    # leitor compartilhado autorizado: 200, sem nome do responsável
    await _enable_aba_flag(db_session)
    consent = await _consent(api_client, auth_headers, patient)
    invited = await _other_professional(db_session, email="equipe@example.com")
    db_session.add(
        PatientCareTeamMember(
            patient_id=patient.id,
            professional_id=invited.id,
            role="practitioner",
            status="active",
            invited_by_professional_id=professional.id,
            consent_event_id=UUID(consent["id"]),
            invited_at=datetime.now(UTC),
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    invited_headers = _professional_headers(invited)
    shared = await api_client.get(base, headers=invited_headers)
    assert shared.status_code == 200, shared.text
    assert shared.json()["items"][0]["actorLabel"] == "Responsável"
    assert "Maria Silva" not in shared.text
    shared_photo = await api_client.get(
        base + f"/{env.check_in_id}/photo/file", headers=invited_headers
    )
    assert shared_photo.status_code == 200

    # profissional sem acesso clínico → 404 nas duas rotas
    stranger = await _other_professional(db_session, email="estranha@example.com")
    stranger_headers = _professional_headers(stranger)
    assert (await api_client.get(base, headers=stranger_headers)).status_code == 404
    assert (
        await api_client.get(
            base + f"/{env.check_in_id}/photo/file", headers=stranger_headers
        )
    ).status_code == 404

    # sem foto vigente a rota privada devolve 404
    removed = await _delete_photo(api_client, env.token, env.check_in_id)
    assert removed.status_code == 200
    assert (
        await api_client.get(
            base + f"/{env.check_in_id}/photo/file", headers=owner_headers
        )
    ).status_code == 404


async def test_delete_photo_is_idempotent_per_request(
    api_client, auth_headers, db_session, patient, professional, fake_storage
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200
    key = fake_storage.uploads[0]
    record_id = uuid.uuid4()

    removed = await _delete_photo(
        api_client,
        env.token,
        env.check_in_id,
        client_record_id=record_id,
        expected_version=1,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"hasPhoto": False, "version": 1}

    replay = await _delete_photo(
        api_client,
        env.token,
        env.check_in_id,
        client_record_id=record_id,
        expected_version=1,
    )
    assert replay.status_code == 200
    assert replay.json() == removed.json()

    again = await _delete_photo(api_client, env.token, env.check_in_id)
    assert again.status_code == 200
    assert again.json() == {"hasPhoto": False, "version": 1}

    stale = await _delete_photo(
        api_client, env.token, env.check_in_id, expected_version=3
    )
    assert stale.status_code == 409

    reused = await _delete_photo(
        api_client,
        env.token,
        env.check_in_id,
        client_record_id=record_id,
        expected_version=2,
    )
    assert reused.status_code == 409

    photo = await db_session.scalar(
        select(HomeProgramPhoto).where(HomeProgramPhoto.check_in_id == env.check_in_id)
    )
    await db_session.refresh(photo)
    assert photo.status == "deleted"

    # o texto permanece e a resposta segue editável na versão original
    row = await db_session.scalar(
        select(HomeProgramCheckIn).where(HomeProgramCheckIn.id == env.check_in_id)
    )
    await db_session.refresh(row)
    assert row.version == 1 and row.done is True
    patched = await api_client.patch(
        f"{PUBLIC}/check-ins/{env.check_in_id}",
        headers=_headers(env.token),
        json={
            "clientRecordId": str(uuid.uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": "Mudou de ideia",
        },
    )
    assert patched.status_code == 200, patched.text

    public = (await api_client.get(PUBLIC, headers=_headers(env.token))).json()
    assert public["tasks"][0]["checkIn"]["hasPhoto"] is False

    summary = await storage_cleanup_service.run_storage_cleanup(db_session)
    assert summary["deleted"] == 1
    assert fake_storage.deletes == [key]
    assert key not in fake_storage.objects


# --------------------------------------------------------------------------- #
# Limites públicos de upload
# --------------------------------------------------------------------------- #


async def test_photo_rate_limits_use_ip_then_grant_budgets(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)
    calls: list[dict] = []

    def fake_allow(*, key, max_requests, window_seconds):
        calls.append(
            {"key": key, "max_requests": max_requests, "window_seconds": window_seconds}
        )
        return True

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", fake_allow
    )

    uploaded = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert uploaded.status_code == 200, uploaded.text
    assert calls[0]["key"].startswith("clinical:home-program-response:ip:")
    assert calls[0]["max_requests"] == 120 and calls[0]["window_seconds"] == 60
    assert calls[1]["key"] == (
        f"clinical:home-program-response:upload:{_sha(str(env.grant.id))}"
    )
    assert calls[1]["max_requests"] == 10 and calls[1]["window_seconds"] == 600
    assert all(env.token not in call["key"] for call in calls)

    calls.clear()
    removed = await _delete_photo(api_client, env.token, env.check_in_id)
    assert removed.status_code == 200
    assert calls[1]["key"] == (
        f"clinical:home-program-response:write:{_sha(str(env.grant.id))}"
    )
    assert calls[1]["max_requests"] == 30 and calls[1]["window_seconds"] == 60

    # novo envio apenas para exercitar a leitura do arquivo (não há foto vigente)
    second = await _upload(
        api_client, env.token, env.check_in_id, jpeg_bytes(color=(9, 9, 9))
    )
    assert second.status_code == 200
    calls.clear()
    read = await api_client.get(
        f"{PUBLIC}/check-ins/{env.check_in_id}/photo/file",
        headers=_headers(env.token),
    )
    assert read.status_code == 200
    assert calls[0]["key"].startswith("clinical:home-program-response:ip:")
    assert calls[1]["key"] == (
        f"clinical:home-program-response:read:{_sha(str(env.grant.id))}"
    )
    assert calls[1]["max_requests"] == 60 and calls[1]["window_seconds"] == 60


async def test_photo_upload_429_and_fail_closed(
    api_client, auth_headers, db_session, patient, professional, fake_storage, monkeypatch
):
    env = await _photo_env(api_client, auth_headers, db_session, patient, professional)

    def deny_upload_only(*, key, max_requests, window_seconds):
        return ":upload:" not in key

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", deny_upload_only
    )
    limited = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After") == "600"
    assert fake_storage.uploads == []

    def _store_down(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", _store_down
    )
    down = await _upload(api_client, env.token, env.check_in_id, jpeg_bytes())
    assert down.status_code == 503
    assert fake_storage.uploads == []
