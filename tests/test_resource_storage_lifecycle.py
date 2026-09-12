"""F17/4.3 — ciclo de vida de blobs de recursos: reserva, órfãos e arquivamento.

Cobre: reserva de limpeza ANTES do upload (retirada na transação que associa o
blob; órfão de queda do processo é removido pelo janitor), chaves imutáveis por
operação dentro de ``resources/{id}/...`` com compatibilidade das chaves
antigas, janitor que nunca remove blob associado/referenciado, substituição e
DELETE bloqueados por referências (409), POST /resources/{id}/archive
idempotente que preserva arquivo/vínculos, bloqueia novas distribuições e
prescrições e revoga a disponibilização externa. SQLite em memória com tabelas
curadas; storage sempre stub local.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.resource_service as resource_service_module
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.attachment import Attachment
from app.models.family_portal_content import FamilyPortalItem
from app.models.home_program import (
    HomeProgramPhoto,
    HomeProgramTaskResource,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.models.resource_link import (
    GoalResourceLink,
    ProgramResourceLink,
    ResourceDomainLink,
)
from app.models.storage_cleanup import (
    STORAGE_CLEANUP_DELETED,
    STORAGE_CLEANUP_PENDING,
    STORAGE_CLEANUP_RESOLVED,
    StorageCleanupTask,
)
from app.services.resource_license_service import (
    ARCHIVED_REASON,
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
)
from app.services.resource_link_service import (
    ResourceLinkConflictError,
    resolve_linkable_resource,
)
from app.services.storage_cleanup_service import run_storage_cleanup

DIGEST = "a" * 64
REASON = "Curadoria conferiu a documentação apresentada."


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Patient.__table__,
                    Resource.__table__,
                    ResourceLicense.__table__,
                    Base.metadata.tables["resource_license_decisions"],
                    ResourceDomainLink.__table__,
                    GoalResourceLink.__table__,
                    ProgramResourceLink.__table__,
                    AdminAuditLog.__table__,
                    Attachment.__table__,
                    StorageCleanupTask.__table__,
                    HomeProgramTaskResource.__table__,
                    HomeProgramPhoto.__table__,
                    FamilyPortalItem.__table__,
                ],
            )
        )
    return engine


async def _pro(
    db: AsyncSession,
    email: str,
    *,
    is_staff: bool = False,
    name: str = "Profissional",
) -> Professional:
    pro = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=name,
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990000",
        is_staff=is_staff,
        email_verified_at=datetime.now(UTC),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


async def _resource(
    db: AsyncSession,
    *,
    owner: Professional | None,
    title: str,
    publication_status: str = "draft",
    storage_key: str | None = None,
) -> Resource:
    resource = Resource(
        owner_professional_id=owner.id if owner else None,
        title=title,
        description="Descrição",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=128,
        author=owner.name if owner else "Equipe KorusFono",
        storage_key=storage_key or f"resources/{uuid.uuid4().hex}/legado.pdf",
        content_type="application/pdf",
        publication_status=publication_status,
    )
    db.add(resource)
    await db.commit()
    await db.refresh(resource)
    return resource


def _headers(pro: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(pro.id)}"}


def _declaration() -> dict:
    return {
        "origin": "original",
        "rightsHolder": "Dona do material",
        "attribution": "Uso autorizado pela autora",
        "allowProfessionalDistribution": True,
        "allowFamilyDelivery": True,
        "declarationAccepted": True,
    }


@pytest.fixture
async def env(monkeypatch):
    engine = await _engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    uploads: list[str] = []
    downloads: list[str] = []
    deletes: list[str] = []
    events: list[tuple[str, str]] = []

    async def fake_upload(key: str, body: bytes, content_type: str) -> str:
        events.append(("upload", key))
        uploads.append(key)
        return key

    async def fake_download(key: str):
        downloads.append(key)
        return b"%PDF-1.4 stub", "application/pdf"

    async def fake_delete(key: str) -> None:
        deletes.append(key)

    monkeypatch.setattr("app.services.resource_service.storage_service.upload", fake_upload)
    monkeypatch.setattr(
        "app.services.resource_service.storage_service.download", fake_download
    )
    monkeypatch.setattr(
        "app.services.resource_service.storage_service.presigned_url",
        AsyncMock(return_value="https://signed.example/file.pdf"),
    )
    monkeypatch.setattr(
        "app.services.storage_cleanup_service.storage_service.delete", fake_delete
    )

    async with factory() as session:
        owner = await _pro(session, "owner@x.com", name="Dona do material")
        other = await _pro(session, "other@x.com", name="Outra profissional")
        staff = await _pro(session, "staff@x.com", is_staff=True, name="Curadoria")
        personal = await _resource(
            session,
            owner=owner,
            title="Material pessoal",
            storage_key=f"resources/{uuid.uuid4().hex}/pessoal-legado.pdf",
        )
        other_item = await _resource(session, owner=other, title="Material da colega")
        global_item = await _resource(session, owner=None, title="Material global")

        async def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "session": session,
                "owner": owner,
                "other": other,
                "staff": staff,
                "personal_item": personal,
                "other_item": other_item,
                "global_item": global_item,
                "uploads": uploads,
                "downloads": downloads,
                "deletes": deletes,
                "events": events,
            }
        app.dependency_overrides.clear()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Reserva antes do upload e chaves imutáveis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_reserves_cleanup_before_upload_and_resolves_on_association(
    env, monkeypatch
):
    client = env["client"]
    owner = env["owner"]
    session = env["session"]

    real_reserve = resource_service_module.reserve_storage_cleanup

    async def spy_reserve(db, storage_key, **kwargs):
        env["events"].append(("reserve", storage_key))
        return await real_reserve(db, storage_key, **kwargs)

    monkeypatch.setattr(resource_service_module, "reserve_storage_cleanup", spy_reserve)

    res = await client.post(
        "/api/v1/resources",
        headers=_headers(owner),
        data={"title": "Com reserva", "categories": '["Linguagem"]'},
        files={"file": ("material.pdf", b"%PDF-1.4 reserva", "application/pdf")},
    )
    assert res.status_code == 201, res.text
    body = res.json()

    # Ordem: reserva primeiro, upload depois — a reserva sobrevive a queda.
    assert len(env["events"]) == 2
    assert env["events"][0][0] == "reserve"
    assert env["events"][1][0] == "upload"
    key = env["events"][0][1]
    assert env["events"][1][1] == key

    # Chave imutável por operação dentro de resources/{id}/...
    parts = key.split("/")
    assert parts[0] == "resources"
    assert parts[1] == body["id"]
    assert len(parts) == 4, "chave deve ter o id da operação entre recurso e arquivo"
    assert parts[3] == "material.pdf"

    resource = await session.get(Resource, uuid.UUID(body["id"]))
    assert resource is not None and resource.storage_key == key

    # A transação que associou o blob retirou a reserva.
    task = (
        await session.execute(
            select(StorageCleanupTask).where(StorageCleanupTask.storage_key == key)
        )
    ).scalar_one()
    assert task.status == STORAGE_CLEANUP_RESOLVED

    # Janitor não toca no blob associado.
    summary = await run_storage_cleanup(session)
    assert summary == {"selected": 0, "deleted": 0, "kept": 0, "retried": 0, "failed": 0}
    assert env["deletes"] == []


@pytest.mark.asyncio
async def test_failed_upload_leaves_committed_orphan_for_janitor(env):
    client = env["client"]
    owner = env["owner"]
    session = env["session"]
    monkeypatch = pytest.MonkeyPatch()
    uploaded: dict = {}

    async def failing_upload(key: str, body: bytes, content_type: str) -> str:
        uploaded["key"] = key
        raise RuntimeError("storage indisponível")

    monkeypatch.setattr(
        "app.services.resource_service.storage_service.upload", failing_upload
    )
    try:
        res = await client.post(
            "/api/v1/resources",
            headers=_headers(owner),
            data={"title": "Vai falhar", "categories": "[]"},
            files={"file": ("material.pdf", b"%PDF-1.4 orfao", "application/pdf")},
        )
        assert res.status_code == 500
    finally:
        monkeypatch.undo()

    key = uploaded["key"]
    assert key.startswith("resources/")

    # A reserva foi committada ANTES do upload: sobrevive ao rollback/queda.
    await session.rollback()
    tasks = (
        (
            await session.execute(
                select(StorageCleanupTask).where(StorageCleanupTask.storage_key == key)
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1 and tasks[0].status == STORAGE_CLEANUP_PENDING
    orphan = (
        await session.execute(select(Resource).where(Resource.storage_key == key))
    ).scalar_one_or_none()
    assert orphan is None, "recurso não é persistido quando o upload falha"

    # O janitor remove o órfão depois — nada de blob esquecido no bucket.
    summary = await run_storage_cleanup(session)
    assert summary["deleted"] == 1
    assert env["deletes"] == [key]


@pytest.mark.asyncio
async def test_replace_uses_immutable_key_and_queues_previous_blob(env):
    client = env["client"]
    owner = env["owner"]
    session = env["session"]
    personal = env["personal_item"]
    previous_key = personal.storage_key

    first = await client.patch(
        f"/api/v1/resources/{personal.id}",
        headers=_headers(owner),
        data={"title": "Material pessoal"},
        files={"file": ("v2.pdf", b"%PDF-1.4 v2", "application/pdf")},
    )
    assert first.status_code == 200, first.text
    second = await client.patch(
        f"/api/v1/resources/{personal.id}",
        headers=_headers(owner),
        data={"title": "Material pessoal"},
        files={"file": ("v3.pdf", b"%PDF-1.4 v3", "application/pdf")},
    )
    assert second.status_code == 200, second.text

    assert len(env["uploads"]) == 2
    assert env["uploads"][0] != env["uploads"][1]
    for key in env["uploads"]:
        parts = key.split("/")
        assert parts[0] == "resources" and parts[1] == str(personal.id) and len(parts) == 4

    await session.refresh(personal)
    assert personal.storage_key == env["uploads"][1]

    pending_keys = {
        task.storage_key
        for task in (
            await session.execute(
                select(StorageCleanupTask).where(
                    StorageCleanupTask.status == STORAGE_CLEANUP_PENDING
                )
            )
        )
        .scalars()
        .all()
    }
    assert pending_keys == {previous_key, env["uploads"][0]}

    summary = await run_storage_cleanup(session)
    assert summary["deleted"] == 2 and summary["kept"] == 0
    assert set(env["deletes"]) == {previous_key, env["uploads"][0]}

    await session.refresh(personal)
    assert personal.storage_key == env["uploads"][1], "blob atual permanece"

    current = (
        await session.execute(
            select(StorageCleanupTask).where(
                StorageCleanupTask.storage_key == env["uploads"][1]
            )
        )
    ).scalar_one()
    assert current.status == STORAGE_CLEANUP_RESOLVED
    assert current.storage_key not in env["deletes"]


@pytest.mark.asyncio
async def test_janitor_keeps_blob_associated_after_lost_reservation(env):
    """Queda entre upload e retirada: reserva perdida, blob associado é preservado."""
    session = env["session"]
    personal = env["personal_item"]

    await resource_service_module.reserve_storage_cleanup(
        session, personal.storage_key, reason="resource_create"
    )

    summary = await run_storage_cleanup(session)

    assert summary["kept"] == 1 and env["deletes"] == []
    task = (
        await session.execute(
            select(StorageCleanupTask).where(
                StorageCleanupTask.storage_key == personal.storage_key
            )
        )
    ).scalar_one()
    assert task.status == "kept"
    await session.refresh(personal)
    assert personal.storage_key


# ---------------------------------------------------------------------------
# Arquivamento
# ---------------------------------------------------------------------------


async def _publish_family_resource(env, resource: Resource) -> tuple[ResourceLicense, GoalResourceLink]:
    session = env["session"]
    owner = env["owner"]
    resource.content_sha256 = DIGEST
    resource.publication_status = "published"
    license_row = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status="approved",
        origin="original",
        rights_holder="Dona do material",
        attribution="",
        allow_professional_distribution=True,
        allow_family_delivery=True,
        content_sha256=DIGEST,
    )
    link = GoalResourceLink(
        goal_id=uuid.uuid4(),
        resource_id=resource.id,
        created_by_professional_id=owner.id,
    )
    session.add_all([license_row, link])
    await session.commit()
    await session.refresh(resource)
    return license_row, link


@pytest.mark.asyncio
async def test_archive_is_idempotent_blocks_delivery_and_revokes_external_visibility(env):
    client = env["client"]
    session = env["session"]
    owner = env["owner"]
    other = env["other"]
    staff = env["staff"]
    personal = env["personal_item"]
    original_key = personal.storage_key

    license_row, link = await _publish_family_resource(env, personal)

    # Pré-condição: visível a terceiros, baixável e com entrega familiar liberada.
    visible = await client.get("/api/v1/resources?scope=all", headers=_headers(other))
    item = next(row for row in visible.json() if row["id"] == str(personal.id))
    assert item["canDeliverToFamily"] is True
    pre_download = await client.get(
        f"/api/v1/resources/{personal.id}/download-url", headers=_headers(other)
    )
    assert pre_download.status_code == 200

    first = await client.post(
        f"/api/v1/resources/{personal.id}/archive", headers=_headers(owner), json={}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["publicationStatus"] == "archived"
    assert body["canDeliverToFamily"] is False
    assert body["unavailableReason"] == ARCHIVED_REASON
    assert body["license"]["status"] == "approved"

    # Idempotente.
    again = await client.post(
        f"/api/v1/resources/{personal.id}/archive", headers=_headers(owner), json={}
    )
    assert again.status_code == 200
    assert again.json()["publicationStatus"] == "archived"

    # Histórico preservado: arquivo, licença e vínculo continuam.
    await session.refresh(personal)
    assert personal.archived_at is not None
    assert personal.storage_key == original_key
    assert (await session.get(ResourceLicense, license_row.id)) is not None
    assert (await session.get(GoalResourceLink, link.id)) is not None

    # Bloqueia novas distribuições/prescrições.
    with pytest.raises(ResourceLinkConflictError) as excinfo:
        await resolve_linkable_resource(session, owner, personal.id)
    assert ARCHIVED_REASON in str(excinfo.value)
    with pytest.raises(ResourceLicensePolicyError):
        assert_can_deliver_to_family(personal, license_row)

    # Revoga a disponibilização externa (catálogo e download de terceiros).
    hidden = await client.get("/api/v1/resources?scope=all", headers=_headers(other))
    assert str(personal.id) not in {row["id"] for row in hidden.json()}
    blocked_download = await client.get(
        f"/api/v1/resources/{personal.id}/download-url", headers=_headers(other)
    )
    assert blocked_download.status_code == 403

    # Re-publicação de arquivado é impedida — exige novo Resource.
    republish = await client.patch(
        f"/api/v1/admin/resources/{personal.id}/publication",
        headers=_headers(staff),
        json={"status": "published", "reason": REASON},
    )
    assert republish.status_code == 409
    # Nova declaração de licença em arquivado também é impedida.
    declare = await client.put(
        f"/api/v1/resources/{personal.id}/license",
        headers=_headers(owner),
        json=_declaration(),
    )
    assert declare.status_code == 409
    # O dono continua acessando o arquivo (histórico não é destruído).
    still_served = await client.get(
        f"/api/v1/resources/{personal.id}/file", headers=_headers(owner)
    )
    assert still_served.status_code == 200


@pytest.mark.asyncio
async def test_archive_requires_ownership(env):
    client = env["client"]
    owner = env["owner"]
    other = env["other"]
    staff = env["staff"]
    personal = env["personal_item"]
    global_item = env["global_item"]

    forbidden = await client.post(
        f"/api/v1/resources/{personal.id}/archive", headers=_headers(other), json={}
    )
    assert forbidden.status_code == 403

    missing = await client.post(
        f"/api/v1/resources/{uuid.uuid4()}/archive", headers=_headers(owner), json={}
    )
    assert missing.status_code == 404

    # Global não tem dono: arquivamento global segue pela curadoria.
    global_archive = await client.post(
        f"/api/v1/resources/{global_item.id}/archive", headers=_headers(staff), json={}
    )
    assert global_archive.status_code == 403


# ---------------------------------------------------------------------------
# DELETE referenciado → 409; sem referência → 204 + limpeza
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_referenced_resource_conflicts_and_unreferenced_queues_cleanup(env):
    client = env["client"]
    session = env["session"]
    owner = env["owner"]

    goal_linked = await _resource(session, owner=owner, title="Vinculada à meta")
    program_linked = await _resource(session, owner=owner, title="Vinculada ao programa")
    licensed = await _resource(session, owner=owner, title="Com licença declarada")
    published = await _resource(
        session, owner=owner, title="Publicada", publication_status="published"
    )
    domain_linked = await _resource(session, owner=owner, title="Com domínio")
    # F14: item do portal (mesmo em rascunho) congela o hash na revisão.
    portal_linked = await _resource(session, owner=owner, title="No portal da família")
    session.add_all(
        [
            GoalResourceLink(
                goal_id=uuid.uuid4(),
                resource_id=goal_linked.id,
                created_by_professional_id=owner.id,
            ),
            ProgramResourceLink(
                program_id=uuid.uuid4(),
                resource_id=program_linked.id,
                created_by_professional_id=owner.id,
            ),
            ResourceLicense(
                resource_id=licensed.id,
                version=1,
                status="declared",
                origin="original",
                rights_holder="Dona do material",
                attribution="",
                content_sha256=DIGEST,
            ),
            ResourceDomainLink(resource_id=domain_linked.id, domain_key="linguagem"),
            FamilyPortalItem(
                portal_id=uuid.uuid4(),
                kind="material",
                status="draft",
                version=1,
                published_version=None,
                draft_content={"title": "No portal", "instructions": ""},
                draft_recipient_ids=[],
                resource_id=portal_linked.id,
                created_by_professional_id=owner.id,
            ),
        ]
    )
    await session.commit()

    for resource in (
        goal_linked,
        program_linked,
        licensed,
        published,
        domain_linked,
        portal_linked,
    ):
        blocked = await client.delete(
            f"/api/v1/resources/{resource.id}", headers=_headers(owner)
        )
        assert blocked.status_code == 409, resource.title
        assert "arquiv" in blocked.json()["detail"]
        assert await session.get(Resource, resource.id) is not None

    orphan = await _resource(session, owner=owner, title="Sem referências")
    orphan_key = orphan.storage_key
    removed = await client.delete(
        f"/api/v1/resources/{orphan.id}", headers=_headers(owner)
    )
    assert removed.status_code == 204
    assert await session.get(Resource, orphan.id) is None

    task = (
        await session.execute(
            select(StorageCleanupTask).where(StorageCleanupTask.storage_key == orphan_key)
        )
    ).scalar_one()
    assert task.status == STORAGE_CLEANUP_PENDING and task.reason == "resource_delete"

    summary = await run_storage_cleanup(session)
    assert summary["deleted"] == 1
    assert env["deletes"] == [orphan_key]
    await session.refresh(task)
    assert task.status == STORAGE_CLEANUP_DELETED


@pytest.mark.asyncio
async def test_replace_blocked_when_resource_has_live_links(env):
    client = env["client"]
    session = env["session"]
    owner = env["owner"]
    personal = env["personal_item"]
    other_item = env["other_item"]

    session.add_all(
        [
            GoalResourceLink(
                goal_id=uuid.uuid4(),
                resource_id=personal.id,
                created_by_professional_id=owner.id,
            ),
            ProgramResourceLink(
                program_id=uuid.uuid4(),
                resource_id=other_item.id,
                created_by_professional_id=env["other"].id,
            ),
        ]
    )
    await session.commit()
    before = {personal.id: personal.storage_key, other_item.id: other_item.storage_key}

    for resource in (personal, other_item):
        blocked = await client.patch(
            f"/api/v1/resources/{resource.id}",
            headers=_headers(env["owner"] if resource is personal else env["other"]),
            data={"title": resource.title},
            files={"file": ("novo.pdf", b"%PDF-1.4 novo", "application/pdf")},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "Cadastre uma nova versão do material."

    assert env["uploads"] == []
    await session.refresh(personal)
    await session.refresh(other_item)
    assert personal.storage_key == before[personal.id]
    assert other_item.storage_key == before[other_item.id]


@pytest.mark.asyncio
async def test_admin_delete_referenced_global_conflicts(env):
    client = env["client"]
    session = env["session"]
    staff = env["staff"]
    global_item = env["global_item"]

    session.add(ResourceDomainLink(resource_id=global_item.id, domain_key="linguagem"))
    await session.commit()

    blocked = await client.delete(
        f"/api/v1/admin/resources/{global_item.id}", headers=_headers(staff)
    )
    assert blocked.status_code == 409
    assert await session.get(Resource, global_item.id) is not None

    orphan = await _resource(session, owner=None, title="Global sem referências")
    orphan_key = orphan.storage_key
    removed = await client.delete(
        f"/api/v1/admin/resources/{orphan.id}", headers=_headers(staff)
    )
    assert removed.status_code == 204
    task = (
        await session.execute(
            select(StorageCleanupTask).where(StorageCleanupTask.storage_key == orphan_key)
        )
    ).scalar_one()
    assert task.status == STORAGE_CLEANUP_PENDING and task.reason == "resource_delete"


@pytest.mark.asyncio
async def test_legacy_storage_keys_remain_served(env):
    """Chaves antigas (sem id de operação) continuam válidas para leitura."""
    client = env["client"]
    personal = env["personal_item"]
    assert len(personal.storage_key.split("/")) == 3

    res = await client.get(
        f"/api/v1/resources/{personal.id}/file", headers=_headers(env["owner"])
    )

    assert res.status_code == 200
    assert env["downloads"] == [personal.storage_key]
