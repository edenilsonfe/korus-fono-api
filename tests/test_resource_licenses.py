"""F17 — licenças de distribuição: declaração, curadoria, publicação e gates.

Cobre: declaração global por admin (pending → aprovação explícita → publicação),
licença autoral privada com entrega familiar expressa, retirada/expiração/hash,
bloqueio de compartilhamento automático, ACL da licença pessoal e leitura em
read-only (entitlement). SQLite em memória; storage é sempre stub local.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense, ResourceLicenseDecision
from app.models.resource_link import ResourceDomainLink
from app.models.storage_cleanup import StorageCleanupTask
from app.services.resource_license_service import (
    ResourceLicensePolicyError,
    assert_can_deliver_to_family,
    assert_can_publish,
)

PDF_BYTES = b"%PDF-1.4 licenca autoral"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()
REASON = "Curadoria conferiu a documentação apresentada."


class StorageStub:
    """Stub do storage: existe/ausente/indisponível, sem rede."""

    def __init__(self) -> None:
        self.available = True
        self.error: Exception | None = None
        self.calls = 0

    async def download_limited(self, key, max_bytes, timeout_seconds=30.0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if not self.available:
            raise FileNotFoundError(key)
        return PDF_BYTES, "application/pdf"


@pytest.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Resource.__table__,
                    ResourceLicense.__table__,
                    ResourceLicenseDecision.__table__,
                    ResourceDomainLink.__table__,
                    StorageCleanupTask.__table__,
                    Base.metadata.tables["admin_audit_logs"],
                ],
            )
        )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stub = StorageStub()
    monkeypatch.setattr(
        "app.services.resource_license_service.storage_service.download_limited",
        stub.download_limited,
    )
    monkeypatch.setattr(
        "app.services.resource_service.storage_service.upload",
        AsyncMock(return_value="resources/mock/file.pdf"),
    )

    async with session_factory() as session:
        owner = await _pro(session, "owner@x.com", name="Dona do material")
        other = await _pro(session, "other@x.com", name="Outra profissional")
        staff = await _pro(session, "staff@x.com", is_staff=True, name="Curadoria")
        support = await _pro(
            session, "support@x.com", admin_role="support", name="Suporte"
        )
        readonly = await _pro(session, "readonly@x.com", name="Plano vencido")
        readonly.subscription_status = "canceled"
        await session.commit()

        async def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "session": session,
                "owner": owner,
                "other": other,
                "staff": staff,
                "support": support,
                "readonly": readonly,
                "storage": stub,
            }
        app.dependency_overrides.clear()
    await engine.dispose()


async def _pro(
    db: AsyncSession,
    email: str,
    *,
    is_staff: bool = False,
    admin_role: str | None = None,
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
        admin_role=admin_role,
        email_verified_at=datetime.now(UTC),
    )
    db.add(pro)
    await db.commit()
    await db.refresh(pro)
    return pro


def _headers(pro: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(pro.id)}"}


async def _global_resource(env, *, title: str = "Material da plataforma") -> Resource:
    session: AsyncSession = env["session"]
    resource = Resource(
        owner_professional_id=None,
        title=title,
        description="Descrição",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=len(PDF_BYTES),
        author="Equipe KorusFono",
        storage_key=f"resources/test/{title}.pdf",
        content_type="application/pdf",
    )
    session.add(resource)
    await session.commit()
    await session.refresh(resource)
    return resource


async def _personal_resource(env, *, title: str = "Material autoral") -> str:
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    res = await client.post(
        "/api/v1/resources",
        headers=_headers(owner),
        data={"title": title, "categories": '["Linguagem"]'},
        files={"file": ("material.pdf", PDF_BYTES, "application/pdf")},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _declaration(**overrides) -> dict:
    payload = {
        "origin": "original",
        "rightsHolder": "Dona do material",
        "attribution": "Uso autorizado pela autora",
        "allowProfessionalDistribution": True,
        "allowFamilyDelivery": True,
        "declarationAccepted": True,
    }
    payload.update(overrides)
    return payload


async def _declare_personal(env, resource_id: str, **overrides) -> dict:
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    res = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(owner),
        json=_declaration(**overrides),
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _declare_global(env, resource_id, **overrides) -> dict:
    client: AsyncClient = env["client"]
    staff: Professional = env["staff"]
    payload = _declaration(
        **{"evidenceReference": "Contrato de cessão 2026/01", **overrides}
    )
    res = await client.post(
        f"/api/v1/admin/resources/{resource_id}/license/declarations",
        headers=_headers(staff),
        json=payload,
    )
    assert res.status_code == 201, res.text
    return res.json()


async def _decide(env, resource_id, license_id, decision="approved", reason=REASON):
    client: AsyncClient = env["client"]
    staff: Professional = env["staff"]
    return await client.put(
        f"/api/v1/admin/resources/{resource_id}/license",
        headers=_headers(staff),
        json={"licenseId": license_id, "decision": decision, "reason": reason},
    )


async def _publish(env, resource_id, status="published", reason=REASON):
    client: AsyncClient = env["client"]
    staff: Professional = env["staff"]
    return await client.patch(
        f"/api/v1/admin/resources/{resource_id}/publication",
        headers=_headers(staff),
        json={"status": status, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Fluxo global: declaração → pendência → aprovação explícita → publicação
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_declaration_starts_pending_without_owner_change(env):
    resource = await _global_resource(env)
    body = await _declare_global(env, resource.id)

    assert body["status"] == "pending"
    assert body["origin"] == "original"
    assert body["declaredByAdmin"] is True
    assert body["declaredByProfessionalId"] == str(env["staff"].id)
    assert body["contentSha256"] == PDF_SHA
    assert body["decisions"] == []
    assert body["evidenceReference"] == "Contrato de cessão 2026/01"

    session: AsyncSession = env["session"]
    await session.refresh(resource)
    assert resource.owner_professional_id is None, "declaração não cria dono artificial"
    assert resource.content_sha256 == PDF_SHA, "hash verificado no storage"
    assert resource.publication_status == "draft"

    audit = (
        await session.execute(
            select(AdminAuditLog).where(AdminAuditLog.action == "declare_resource_license")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_id == env["staff"].id


@pytest.mark.asyncio
async def test_global_approval_then_publication_and_archive(env):
    resource = await _global_resource(env)
    declaration = await _declare_global(env, resource.id)

    # Sem aprovação explícita não há publicação (nem implícita).
    blocked = await _publish(env, resource.id)
    assert blocked.status_code == 409
    assert "revisão" in blocked.json()["detail"]

    approved = await _decide(env, resource.id, declaration["id"])
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert [d["decision"] for d in body["decisions"]] == ["approved"]
    assert body["decisions"][0]["reason"] == REASON

    published = await _publish(env, resource.id)
    assert published.status_code == 200
    assert published.json()["publicationStatus"] == "published"
    assert published.json()["license"]["status"] == "approved"

    # Outra profissional vê o material publicado no catálogo e baixa.
    other_client: AsyncClient = env["client"]
    listing = await other_client.get(
        "/api/v1/resources?scope=global", headers=_headers(env["other"])
    )
    titles = {item["title"] for item in listing.json()}
    assert "Material da plataforma" in titles
    download = await other_client.get(
        f"/api/v1/resources/{resource.id}/download-url", headers=_headers(env["other"])
    )
    assert download.status_code == 200

    # Arquivar bloqueia novos acessos e impede republicação.
    archived = await _publish(env, resource.id, status="archived")
    assert archived.status_code == 200
    assert archived.json()["publicationStatus"] == "archived"

    session: AsyncSession = env["session"]
    await session.refresh(resource)
    assert resource.archived_at is not None

    listing = await other_client.get(
        "/api/v1/resources?scope=global", headers=_headers(env["other"])
    )
    assert "Material da plataforma" not in {item["title"] for item in listing.json()}

    file_res = await other_client.get(
        f"/api/v1/resources/{resource.id}/file", headers=_headers(env["other"])
    )
    assert file_res.status_code == 403

    republish = await _publish(env, resource.id)
    assert republish.status_code == 409
    assert "arquivado" in republish.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_declaration_requires_evidence_and_global_material(env):
    client: AsyncClient = env["client"]
    staff: Professional = env["staff"]

    personal_id = await _personal_resource(env)
    personal = await client.post(
        f"/api/v1/admin/resources/{personal_id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(evidenceReference="Contrato 1"),
    )
    assert personal.status_code == 409
    assert "próprio dono" in personal.json()["detail"]

    resource = await _global_resource(env)
    no_evidence = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(),
    )
    assert no_evidence.status_code == 422

    blank_holder = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(rightsHolder="   ", evidenceReference="Contrato 2"),
    )
    assert blank_holder.status_code == 422

    missing_accept = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json={"origin": "original", "rightsHolder": "X", "evidenceReference": "Y"},
    )
    assert missing_accept.status_code == 422

    extra = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(evidenceReference="Contrato 3", unexpectedField=True),
    )
    assert extra.status_code == 422

    past_expiry = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(evidenceReference="Contrato 4", validUntil="2000-01-01"),
    )
    assert past_expiry.status_code == 422

    created = await _declare_global(env, resource.id)
    stale = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(staff),
        json=_declaration(
            evidenceReference="Contrato 5", expectedVersion=created["version"] + 1
        ),
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_admin_permissions_read_vs_write(env):
    client: AsyncClient = env["client"]
    support: Professional = env["support"]
    resource = await _global_resource(env)

    listing = await client.get("/api/v1/admin/resources", headers=_headers(support))
    assert listing.status_code == 200

    read_license = await client.get(
        f"/api/v1/admin/resources/{resource.id}/license", headers=_headers(support)
    )
    assert read_license.status_code == 200
    assert read_license.json() is None

    declare = await client.post(
        f"/api/v1/admin/resources/{resource.id}/license/declarations",
        headers=_headers(support),
        json=_declaration(evidenceReference="Contrato 9"),
    )
    assert declare.status_code == 403

    decide = await client.put(
        f"/api/v1/admin/resources/{resource.id}/license",
        headers=_headers(support),
        json={"licenseId": str(resource.id), "decision": "approved", "reason": REASON},
    )
    assert decide.status_code == 403

    publish = await client.patch(
        f"/api/v1/admin/resources/{resource.id}/publication",
        headers=_headers(support),
        json={"status": "published", "reason": REASON},
    )
    assert publish.status_code == 403

    # Profissional comum não entra no console admin.
    professional = await client.get(
        "/api/v1/admin/resources", headers=_headers(env["owner"])
    )
    assert professional.status_code == 403


# ---------------------------------------------------------------------------
# Licença pessoal (dono)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_authorial_declaration_allows_family_and_keeps_history(env):
    resource_id = await _personal_resource(env)
    first = await _declare_personal(env, resource_id)
    assert first["status"] == "declared"
    assert first["version"] == 1
    assert first["declaredByAdmin"] is False
    assert first["declaredByProfessionalId"] == str(env["owner"].id)
    assert first["allowFamilyDelivery"] is True

    # DTO do recurso: licença declarada + entrega familiar habilitada, ainda draft.
    client: AsyncClient = env["client"]
    listing = await client.get("/api/v1/resources?scope=mine", headers=_headers(env["owner"]))
    item = listing.json()[0]
    assert item["publicationStatus"] == "draft"
    assert item["license"]["status"] == "declared"
    assert item["canDeliverToFamily"] is True
    assert item["unavailableReason"] is None
    # Sem comprovação administrativa/storage no DTO de recurso.
    assert "storageKey" not in item
    assert set(item["license"].keys()) == {
        "id",
        "version",
        "status",
        "origin",
        "rightsHolder",
        "attribution",
        "validUntil",
        "allowProfessionalDistribution",
        "allowFamilyDelivery",
    }

    # Nova declaração substitui a concessão atual preservando o histórico.
    second = await _declare_personal(
        env, resource_id, attribution="Versão revisada", expectedVersion=1
    )
    assert second["version"] == 2
    assert second["attribution"] == "Versão revisada"

    session: AsyncSession = env["session"]
    rows = (
        await session.execute(
            select(ResourceLicense).order_by(ResourceLicense.version)
        )
    ).scalars().all()
    assert [row.version for row in rows] == [1, 2]
    assert rows[0].status == "declared"  # histórico preservado

    # Versão obsoleta -> 409.
    stale = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(env["owner"]),
        json=_declaration(expectedVersion=1),
    )
    assert stale.status_code == 409

    current = await client.get(
        f"/api/v1/resources/{resource_id}/license", headers=_headers(env["owner"])
    )
    assert current.status_code == 200
    assert current.json()["version"] == 2


@pytest.mark.asyncio
async def test_authorial_personal_can_be_approved_and_published(env):
    """Autoral privado do dono entra no catálogo só após aprovação + publicação."""
    resource_id = await _personal_resource(env, title="Material autoral")
    declaration = await _declare_personal(env, resource_id)

    approved = await _decide(env, resource_id, declaration["id"])
    assert approved.status_code == 200

    published = await _publish(env, resource_id)
    assert published.status_code == 200
    assert published.json()["publicationStatus"] == "published"
    assert published.json()["license"]["status"] == "approved"

    client: AsyncClient = env["client"]
    owner_view = await client.get(
        f"/api/v1/resources/{resource_id}/license", headers=_headers(env["owner"])
    )
    assert owner_view.status_code == 200
    assert owner_view.json()["status"] == "approved"
    assert owner_view.json()["decisions"][0]["reason"] == REASON

    listing = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(env["other"])
    )
    assert any(item["title"] == "Material autoral" for item in listing.json())


@pytest.mark.asyncio
async def test_personal_license_is_owner_scoped(env):
    client: AsyncClient = env["client"]
    resource_id = await _personal_resource(env)

    other_get = await client.get(
        f"/api/v1/resources/{resource_id}/license", headers=_headers(env["other"])
    )
    assert other_get.status_code == 404

    other_put = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(env["other"]),
        json=_declaration(),
    )
    assert other_put.status_code == 404

    missing = await client.get(
        "/api/v1/resources/00000000-0000-0000-0000-000000000000/license",
        headers=_headers(env["owner"]),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_third_party_origin_goes_pending_and_needs_review(env):
    resource_id = await _personal_resource(env)
    third_party = await _declare_personal(
        env,
        resource_id,
        origin="licensed",
        rightsHolder="Editora Exemplo",
        sourceReference="NF 123",
    )
    assert third_party["status"] == "pending"
    assert third_party["sourceReference"] == "NF 123"

    client: AsyncClient = env["client"]
    listing = await client.get("/api/v1/resources?scope=mine", headers=_headers(env["owner"]))
    item = listing.json()[0]
    assert item["license"]["status"] == "pending"
    assert item["canDeliverToFamily"] is False
    assert "revisão" in item["unavailableReason"]

    detail = await client.get(
        f"/api/v1/resources/{resource_id}/license", headers=_headers(env["owner"])
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"
    assert detail.json()["evidenceReference"] is None


@pytest.mark.asyncio
async def test_personal_declaration_requires_storage_verification(env):
    resource_id = await _personal_resource(env)
    client: AsyncClient = env["client"]

    env["storage"].error = RuntimeError("s3 fora do ar")
    down = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(env["owner"]),
        json=_declaration(),
    )
    assert down.status_code == 503

    env["storage"].error = None
    env["storage"].available = False
    missing = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(env["owner"]),
        json=_declaration(),
    )
    assert missing.status_code == 409
    assert "Arquivo do material indisponível" in missing.json()["detail"]


# ---------------------------------------------------------------------------
# Publicação e gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_requires_approved_license_and_available_blob(env):
    resource = await _global_resource(env)

    no_license = await _publish(env, resource.id)
    assert no_license.status_code == 409
    assert "sem declaração de licença" in no_license.json()["detail"]

    declaration = await _declare_global(env, resource.id)
    pending_publish = await _publish(env, resource.id)
    assert pending_publish.status_code == 409

    assert (await _decide(env, resource.id, declaration["id"])).status_code == 200

    env["storage"].available = False
    missing_blob = await _publish(env, resource.id)
    assert missing_blob.status_code == 409
    assert "indisponível" in missing_blob.json()["detail"]

    env["storage"].available = True
    env["storage"].error = RuntimeError("s3 fora do ar")
    storage_down = await _publish(env, resource.id)
    assert storage_down.status_code == 503

    env["storage"].error = None
    ok = await _publish(env, resource.id)
    assert ok.status_code == 200
    assert ok.json()["publicationStatus"] == "published"

    # Republicar é idempotente enquanto a licença continua válida.
    again = await _publish(env, resource.id)
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_publish_blocked_when_content_diverges_from_license(env):
    resource = await _global_resource(env)
    declaration = await _declare_global(env, resource.id)
    await _decide(env, resource.id, declaration["id"])

    session: AsyncSession = env["session"]
    resource.content_sha256 = "b" * 64
    session.add(resource)
    await session.commit()

    blocked = await _publish(env, resource.id)
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_archived_resource_rejects_new_declarations(env):
    resource_id = await _personal_resource(env)
    archived = await _publish(env, resource_id, status="archived")
    assert archived.status_code == 200

    client: AsyncClient = env["client"]
    res = await client.put(
        f"/api/v1/resources/{resource_id}/license",
        headers=_headers(env["owner"]),
        json=_declaration(),
    )
    assert res.status_code == 409
    assert "arquivado" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_family_delivery_gate_expiry_and_revocation(env):
    resource_id = await _personal_resource(env)
    await _declare_personal(env, resource_id)
    client: AsyncClient = env["client"]

    async def family_state() -> dict:
        listing = await client.get(
            "/api/v1/resources?scope=mine", headers=_headers(env["owner"])
        )
        return listing.json()[0]

    assert (await family_state())["canDeliverToFamily"] is True

    # Retirada pela curadoria bloqueia a entrega familiar.
    session: AsyncSession = env["session"]
    license_row = (
        await session.execute(select(ResourceLicense))
    ).scalar_one()
    revoked = await _decide(
        env, resource_id, str(license_row.id), decision="revoked", reason=REASON
    )
    assert revoked.status_code == 200
    state = await family_state()
    assert state["canDeliverToFamily"] is False
    assert state["unavailableReason"] == "Licença revogada."

    # Expiração é derivada de validUntil (sem job de aprovação/expiração).
    license_row.valid_until = date(2000, 1, 1)
    license_row.status = "declared"
    session.add(license_row)
    await session.commit()
    state = await family_state()
    assert state["canDeliverToFamily"] is False
    assert state["unavailableReason"] == "Licença expirada."

    # Hash divergente invalida a licença (isolado da expiração).
    license_row.valid_until = None
    session.add(license_row)
    resource = (
        await session.execute(
            select(Resource).where(Resource.id == uuid.UUID(resource_id))
        )
    ).scalar_one()
    resource.content_sha256 = "c" * 64
    await session.commit()
    state = await family_state()
    assert state["canDeliverToFamily"] is False
    assert state["unavailableReason"] == "Licença não corresponde ao arquivo atual do material."


@pytest.mark.asyncio
async def test_declared_without_family_permission_blocks_family_only(env):
    resource_id = await _personal_resource(env)
    body = await _declare_personal(env, resource_id, allowFamilyDelivery=False)
    assert body["status"] == "declared"

    client: AsyncClient = env["client"]
    listing = await client.get("/api/v1/resources?scope=mine", headers=_headers(env["owner"]))
    item = listing.json()[0]
    assert item["canDeliverToFamily"] is False
    assert item["unavailableReason"] == "Licença sem permissão de entrega à família."


@pytest.mark.asyncio
async def test_decision_transitions_and_errors(env):
    resource = await _global_resource(env)
    declaration = await _declare_global(env, resource.id)

    # Licença inexistente -> 404.
    unknown = await _decide(
        env, resource.id, "00000000-0000-0000-0000-000000000000"
    )
    assert unknown.status_code == 404

    # Motivo curto -> 422 (schema).
    short_reason = await _decide(env, resource.id, declaration["id"], reason="no")
    assert short_reason.status_code == 422

    assert (await _decide(env, resource.id, declaration["id"])).status_code == 200

    # Nova decisão sobre licença aprovada só aceita revogação.
    again = await _decide(env, resource.id, declaration["id"], decision="approved")
    assert again.status_code == 409
    assert "não aceita nova decisão" in again.json()["detail"]

    assert (
        await _decide(env, resource.id, declaration["id"], decision="revoked")
    ).status_code == 200
    after_revoke = await _decide(
        env, resource.id, declaration["id"], decision="approved"
    )
    assert after_revoke.status_code == 409

    # Versão antiga não aceita decisão (vigente é a nova).
    new_decl = await _declare_global(
        env, resource.id, evidenceReference="Contrato 7", expectedVersion=1
    )
    old_decision = await _decide(env, resource.id, declaration["id"])
    assert old_decision.status_code == 409
    assert "versão vigente" in old_decision.json()["detail"]

    rejected = await _decide(
        env, resource.id, new_decl["id"], decision="rejected", reason=REASON
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["decisions"][-1]["decision"] == "rejected"
    assert body["decisions"][-1]["reason"] == REASON


@pytest.mark.asyncio
async def test_read_only_entitlement_allows_reads_but_not_mutations(env):
    """Read-only permite o que já era permitido; declarar/publicar/editar não."""
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    readonly: Professional = env["readonly"]

    resource = Resource(
        owner_professional_id=readonly.id,
        title="Consulta em read-only",
        description="",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=len(PDF_BYTES),
        author=readonly.name,
        storage_key="resources/readonly/consulta.pdf",
        content_type="application/pdf",
    )
    session.add(resource)
    await session.commit()
    await session.refresh(resource)

    listing = await client.get(
        "/api/v1/resources?scope=mine", headers=_headers(readonly)
    )
    assert listing.status_code == 200
    assert {item["title"] for item in listing.json()} == {"Consulta em read-only"}

    license_get = await client.get(
        f"/api/v1/resources/{resource.id}/license", headers=_headers(readonly)
    )
    assert license_get.status_code == 200
    assert license_get.json() is None

    # Declarar licença é mutação: bloqueada pelo entitlement, sem efeito colateral.
    declare = await client.put(
        f"/api/v1/resources/{resource.id}/license",
        headers=_headers(readonly),
        json=_declaration(),
    )
    assert declare.status_code == 403
    assert declare.json().get("type") == "entitlement_error"

    create = await client.post(
        "/api/v1/resources",
        headers=_headers(readonly),
        data={"title": "Bloqueado", "categories": "[]"},
        files={"file": ("x.pdf", PDF_BYTES, "application/pdf")},
    )
    assert create.status_code == 403
    assert create.json().get("type") == "entitlement_error"

    await session.refresh(resource)
    assert resource.publication_status == "draft"

    # Arquivar o próprio material é ação protetiva: permanece disponível em
    # read-only (F17 — bloqueia novas distribuições sem destruir histórico).
    archive = await client.post(
        f"/api/v1/resources/{resource.id}/archive",
        headers=_headers(readonly),
        json={},
    )
    assert archive.status_code == 200
    assert archive.json()["publicationStatus"] == "archived"
    await session.refresh(resource)
    assert resource.publication_status == "archived"


# ---------------------------------------------------------------------------
# Unidades dos gates (interfaces consumidas pela F16)
# ---------------------------------------------------------------------------


def test_assert_gates_message_matrix():
    resource = Resource(
        title="X",
        description="",
        categories=[],
        format="PDF",
        file_size_bytes=1,
        storage_key="resources/x.pdf",
        content_type="application/pdf",
        publication_status="draft",
        content_sha256="d" * 64,
    )
    with pytest.raises(ResourceLicensePolicyError):
        assert_can_publish(resource, None)
    with pytest.raises(ResourceLicensePolicyError):
        assert_can_deliver_to_family(resource, None)

    license_row = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status="approved",
        origin="original",
        rights_holder="Titular",
        attribution="",
        allow_professional_distribution=True,
        allow_family_delivery=True,
        content_sha256="d" * 64,
    )
    assert_can_publish(resource, license_row)
    assert_can_deliver_to_family(resource, license_row)

    license_row.allow_family_delivery = False
    with pytest.raises(ResourceLicensePolicyError, match="entrega à família"):
        assert_can_deliver_to_family(resource, license_row)

    license_row.status = "pending"
    with pytest.raises(ResourceLicensePolicyError):
        assert_can_publish(resource, license_row)

    resource.publication_status = "archived"
    with pytest.raises(ResourceLicensePolicyError, match="arquivado"):
        assert_can_deliver_to_family(resource, license_row)
