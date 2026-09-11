"""F16 fase 3 — materiais da tarefa do programa de casa (Tarefa 5.3).

Cobre o contrato do plano §3.5: até cinco recursos por tarefa fixados na
prescrição (título/licença/checksum), entrega do ARQUIVO à família somente com
grant válido + vínculo exato + versão congelada + ``assert_can_deliver_to_family``
revalidado a CADA entrega (retirada de licença bloqueia o arquivo sem apagar
instruções nem resposta), 404 fora do escopo/global sem vínculo, 503/404 de
storage, 410 de link rotacionado e o bloqueio 409 de excluir/substituir um
``Resource`` vinculado a tarefa de programa de casa (``resource_has_references``
ampliado). SQLite em memória; storage sempre stub local.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.caregiver import Caregiver
from app.models.goal import Goal
from app.models.home_program import HomeProgramGrant, HomeProgramTaskResource
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.services.resource_service import resource_has_references

TODAY = date.today()
TOKEN_HEADER = "X-Home-Program-Token"
PUBLIC = "/api/v1/home-program-responses"
PDF_BYTES = b"%PDF-1.4 conteudo-do-material-familiar"


def _headers(token: str | None) -> dict[str, str]:
    return {} if token is None else {TOKEN_HEADER: token}


class FakeMissingKeyError(FileNotFoundError):
    """Objeto ausente no storage falso (mesma semântica do S3 NoSuchKey)."""


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: contador público sempre 'permite'."""
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


@pytest.fixture
def fake_material_storage(monkeypatch):
    """Storage em memória só para a leitura do arquivo do material."""
    objects: dict[str, bytes] = {}
    content_types: dict[str, str] = {}

    async def fake_download_limited(
        key: str, max_bytes: int, timeout_seconds: float = 30.0
    ) -> tuple[bytes, str | None]:
        if key not in objects:
            raise FakeMissingKeyError(key)
        return objects[key], content_types.get(key)

    async def forbidden_presign(*args, **kwargs):
        raise AssertionError("material familiar nunca usa presigned URL")

    monkeypatch.setattr(
        "app.services.home_program_photo_service.storage_service.download_limited",
        fake_download_limited,
    )
    monkeypatch.setattr(
        "app.services.storage.storage_service.presigned_url", forbidden_presign
    )
    return SimpleNamespace(
        objects=objects,
        content_types=content_types,
        download_limited=fake_download_limited,
    )


def _seed_material(fake_material_storage, resource: Resource) -> None:
    fake_material_storage.objects[resource.storage_key] = PDF_BYTES
    fake_material_storage.content_types[resource.storage_key] = "application/pdf"


# --------------------------------------------------------------------------- #
# Fixtures de programa/grant/resource (mesmos padrões das Tarefas 5.1/5.2)
# --------------------------------------------------------------------------- #


def _task(*, goal_id: UUID, resource_ids: list[UUID] | None = None) -> dict:
    return {
        "title": "Usar os cartões",
        "instructions": "Mostre os cartões e aponte os animais.",
        "dueOn": TODAY.isoformat(),
        "goalId": str(goal_id),
        "interventionProgramId": None,
        "resourceIds": [str(resource_id) for resource_id in (resource_ids or [])],
    }


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


async def _resource(
    db_session,
    professional: Professional,
    *,
    family: bool = True,
    title: str = "Cartões de animais",
    storage_key: str | None = None,
) -> tuple[Resource, ResourceLicense]:
    sha = "a" * 64
    resource = Resource(
        owner_professional_id=professional.id,
        title=title,
        description="",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=100,
        author=professional.name,
        storage_key=storage_key or f"resources/test/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
        content_sha256=sha,
    )
    db_session.add(resource)
    await db_session.flush()
    license_row = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status="declared",
        origin="original",
        rights_holder=professional.name,
        attribution="Uso autorizado pela autora",
        allow_professional_distribution=True,
        allow_family_delivery=family,
        content_sha256=sha,
    )
    db_session.add(license_row)
    await db_session.commit()
    await db_session.refresh(resource)
    await db_session.refresh(license_row)
    return resource, license_row


async def _published_program(
    api_client,
    auth_headers,
    patient: Patient,
    db_session,
    professional: Professional,
    *,
    resource_ids: list[UUID] | None = None,
    goal: Goal | None = None,
) -> dict:
    goal = goal or await _goal(db_session, patient, professional)
    body = {
        "title": "Rotina da casa",
        "startsOn": TODAY.isoformat(),
        "endsOn": (TODAY + timedelta(days=13)).isoformat(),
        "tasks": [_task(goal_id=goal.id, resource_ids=resource_ids or [])],
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


async def _material_env(
    api_client, auth_headers, db_session, patient, professional, fake_storage
) -> SimpleNamespace:
    resource, license_row = await _resource(db_session, professional)
    _seed_material(fake_storage, resource)
    program = await _published_program(
        api_client,
        auth_headers,
        patient,
        db_session,
        professional,
        resource_ids=[resource.id],
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    return SimpleNamespace(
        resource=resource,
        license=license_row,
        program=program,
        task_id=program["tasks"][0]["id"],
        token=token,
        grant=grant,
    )


# --------------------------------------------------------------------------- #
# Entrega do arquivo do material
# --------------------------------------------------------------------------- #


async def test_public_material_file_serves_linked_bytes(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )

    response = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(env.token),
    )
    assert response.status_code == 200, response.text
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" not in response.headers["content-disposition"]
    assert "inline" in response.headers["content-disposition"]

    # entregar à família não é "download" do profissional: métrica intacta
    await db_session.refresh(env.resource)
    assert env.resource.downloads == 0

    # sem token → 410 genérico
    anonymous = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file"
    )
    assert anonymous.status_code == 410


async def test_material_file_revalidates_license_on_every_delivery(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    url = f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file"

    first = await api_client.get(url, headers=_headers(env.token))
    assert first.status_code == 200

    # retirada de licença bloqueia o ARQUIVO (mas não apaga instruções/resposta)
    env.license.status = "revoked"
    await db_session.commit()
    blocked = await api_client.get(url, headers=_headers(env.token))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == (
        "Este material não está disponível para a família no momento."
    )

    public = (
        await api_client.get(PUBLIC, headers=_headers(env.token))
    ).json()
    materials = public["tasks"][0]["materials"]
    assert materials[0]["id"] == str(env.resource.id)
    assert materials[0]["title"] == "Cartões de animais"
    assert materials[0]["available"] is False
    assert public["tasks"][0]["instructions"]  # instruções continuam

    # nova declaração familiar reabre a entrega (revalidado a cada GET)
    db_session.add(
        ResourceLicense(
            resource_id=env.resource.id,
            version=2,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            attribution="Uso autorizado pela autora",
            allow_professional_distribution=True,
            allow_family_delivery=True,
            content_sha256=env.resource.content_sha256,
        )
    )
    await db_session.commit()
    reopened = await api_client.get(url, headers=_headers(env.token))
    assert reopened.status_code == 200, reopened.text

    # licença expirada também bloqueia
    db_session.add(
        ResourceLicense(
            resource_id=env.resource.id,
            version=3,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            allow_professional_distribution=True,
            allow_family_delivery=True,
            content_sha256=env.resource.content_sha256,
            valid_until=TODAY - timedelta(days=1),
        )
    )
    await db_session.commit()
    expired = await api_client.get(url, headers=_headers(env.token))
    assert expired.status_code == 409


async def test_material_file_requires_exact_link_and_scope(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    r2, _ = await _resource(db_session, professional, title="Outro material")
    _seed_material(fake_material_storage, r2)
    program_b = await _published_program(
        api_client,
        auth_headers,
        patient,
        db_session,
        professional,
        resource_ids=[r2.id],
    )
    task_b = program_b["tasks"][0]["id"]

    # recurso não vinculado à tarefa → 404 (mesmo sendo do dono)
    not_linked = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{r2.id}/file",
        headers=_headers(env.token),
    )
    assert not_linked.status_code == 404

    # tarefa de outro programa (fora do grant) → 404
    foreign_task = await api_client.get(
        f"{PUBLIC}/tasks/{task_b}/materials/{env.resource.id}/file",
        headers=_headers(env.token),
    )
    assert foreign_task.status_code == 404

    # um grant por programa: o link B só entrega o material de B
    token_b, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program_b
    )
    delivered_b = await api_client.get(
        f"{PUBLIC}/tasks/{task_b}/materials/{r2.id}/file",
        headers=_headers(token_b),
    )
    assert delivered_b.status_code == 200, delivered_b.text
    cross = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(token_b),
    )
    assert cross.status_code == 404

    # recurso global sem vínculo não é exposto (nada de catálogo sem auth)
    global_resource = Resource(
        owner_professional_id=None,
        title="Material global",
        description="",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=10,
        author="Equipe KorusFono",
        storage_key=f"resources/global/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
        content_sha256="c" * 64,
    )
    db_session.add(global_resource)
    await db_session.commit()
    await db_session.refresh(global_resource)
    _seed_material(fake_material_storage, global_resource)
    global_hit = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{global_resource.id}/file",
        headers=_headers(env.token),
    )
    assert global_hit.status_code == 404

    # rotação revoga o token antigo do programa A → 410
    rotated_token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, env.program
    )
    old = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(env.token),
    )
    assert old.status_code == 410
    fresh = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(rotated_token),
    )
    assert fresh.status_code == 200


async def test_material_file_blocks_frozen_checksum_mismatch(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    url = f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file"

    # licença continua válida para o NOVO conteúdo; o que diverge é o checksum
    # congelado no vínculo da tarefa (versão antiga não é servida por baixo)
    env.resource.content_sha256 = "b" * 64
    env.license.content_sha256 = "b" * 64
    await db_session.commit()

    blocked = await api_client.get(url, headers=_headers(env.token))
    assert blocked.status_code == 409, blocked.text


async def test_material_file_storage_failures(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage, monkeypatch
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    url = f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file"

    fake_material_storage.objects.pop(env.resource.storage_key)
    missing = await api_client.get(url, headers=_headers(env.token))
    assert missing.status_code == 404

    import app.services.home_program_photo_service as photo_service_module

    async def broken_download(*args, **kwargs):
        raise RuntimeError("storage fora do ar")

    monkeypatch.setattr(
        photo_service_module.storage_service, "download_limited", broken_download
    )
    unavailable = await api_client.get(url, headers=_headers(env.token))
    assert unavailable.status_code == 503
    assert "indisponível" in unavailable.json()["detail"]


async def test_material_file_uses_read_rate_limit(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage, monkeypatch
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    calls: list[dict] = []

    def fake_allow(*, key, max_requests, window_seconds):
        calls.append(
            {"key": key, "max_requests": max_requests, "window_seconds": window_seconds}
        )
        return True

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", fake_allow
    )
    response = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(env.token),
    )
    assert response.status_code == 200
    assert calls[0]["key"].startswith("clinical:home-program-response:ip:")
    assert calls[0]["max_requests"] == 120
    assert calls[1]["key"].startswith("clinical:home-program-response:read:")
    assert calls[1]["max_requests"] == 60
    assert all(env.token not in call["key"] for call in calls)


# --------------------------------------------------------------------------- #
# Resource vinculado: arquivo fixado na prescrição
# --------------------------------------------------------------------------- #


async def test_linked_resource_cannot_be_deleted_or_replaced(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage, monkeypatch
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )

    # sem vínculo de meta/programa ABA e sem licença no caminho base, a única
    # referência viva é a tarefa do programa de casa (5.3)
    assert await resource_has_references(db_session, env.resource) is True
    link = await db_session.scalar(
        select(HomeProgramTaskResource).where(
            HomeProgramTaskResource.resource_id == env.resource.id
        )
    )
    assert link is not None
    await db_session.refresh(env.resource)

    uploads: list[str] = []

    async def spy_upload(key, body, content_type):
        uploads.append(key)
        return key

    monkeypatch.setattr(
        "app.services.resource_service.storage_service.upload", spy_upload
    )

    replaced = await api_client.patch(
        f"/api/v1/resources/{env.resource.id}",
        headers=auth_headers,
        data={"title": env.resource.title},
        files={"file": ("novo.pdf", b"%PDF-1.4 novo", "application/pdf")},
    )
    assert replaced.status_code == 409, replaced.text
    assert uploads == []

    removed = await api_client.delete(
        f"/api/v1/resources/{env.resource.id}", headers=auth_headers
    )
    assert removed.status_code == 409, removed.text
    assert "referenciado" in removed.json()["detail"]

    # o material (e o arquivo) seguem intactos e entregáveis
    await db_session.refresh(env.resource)
    assert env.resource.storage_key in fake_material_storage.objects
    still = await api_client.get(
        f"{PUBLIC}/tasks/{env.task_id}/materials/{env.resource.id}/file",
        headers=_headers(env.token),
    )
    assert still.status_code == 200

    count = await db_session.scalar(select(func.count()).select_from(Resource))
    assert count == 1  # só o material deste teste


async def test_material_uses_the_frozen_snapshot_title(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    env = await _material_env(
        api_client, auth_headers, db_session, patient, professional, fake_material_storage
    )
    env.resource.title = "Título renomeado depois"
    await db_session.commit()

    public = (await api_client.get(PUBLIC, headers=_headers(env.token))).json()
    materials = public["tasks"][0]["materials"]
    assert materials[0]["title"] == "Cartões de animais"
