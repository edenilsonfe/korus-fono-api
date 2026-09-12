"""Resources library — ownership, mime validation, admin gate."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import engine as _real_engine
from app.db.session import get_db
from app.main import app
from app.models.family_portal_content import FamilyPortalItem
from app.models.home_program import HomeProgramTaskResource
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense, ResourceLicenseDecision
from app.models.resource_link import (
    GoalResourceLink,
    ProgramResourceLink,
    ResourceDomainLink,
)
from app.models.storage_cleanup import StorageCleanupTask

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
async def _patch_middleware_db(monkeypatch):
    """EntitlementMiddleware usa AsyncSessionLocal (engine Postgres real) direto,
    e o pytest-asyncio troca o event loop a cada teste — conexões asyncpg
    reaproveitadas entre loops quebram. Aponta o middleware para um sqlite em
    memória vazio (novo por teste), mantendo a semântica: profissional ausente
    -> middleware deixa a requisição passar."""
    # Solta conexões asyncpg órfãs de outros arquivos de teste (loop fechado)
    # sem tentar fechá-las — evita GC tardio estourando no loop deste teste.
    await _real_engine.dispose(close=False)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", maker)
    yield


async def _engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Resource.__table__,
                    ResourceLicense.__table__,
                    ResourceLicenseDecision.__table__,
                    ResourceDomainLink.__table__,
                    GoalResourceLink.__table__,
                    ProgramResourceLink.__table__,
                    HomeProgramTaskResource.__table__,
                    StorageCleanupTask.__table__,
                    FamilyPortalItem.__table__,
                    Base.metadata.tables["admin_audit_logs"],
                ],
            )
        )
    return eng


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
    title: str = "Material",
) -> Resource:
    resource = Resource(
        owner_professional_id=owner.id if owner else None,
        title=title,
        description="Descrição",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=128,
        author=owner.name if owner else "Equipe KorusFono",
        storage_key=f"resources/test/{title}.pdf",
        content_type="application/pdf",
    )
    db.add(resource)
    await db.commit()
    await db.refresh(resource)
    return resource


async def _publish_with_approved_license(
    db: AsyncSession,
    resource: Resource,
    *,
    allow_professional: bool = True,
    allow_family: bool = False,
) -> ResourceLicense:
    """Atalho de setup: licença aprovada + hash casado + publicado (visível a terceiros)."""
    digest = "a" * 64
    resource.content_sha256 = digest
    resource.publication_status = "published"
    license = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status="approved",
        origin="original",
        rights_holder="Titular dos direitos",
        attribution="",
        allow_professional_distribution=allow_professional,
        allow_family_delivery=allow_family,
        content_sha256=digest,
    )
    db.add(license)
    await db.commit()
    await db.refresh(license)
    await db.refresh(resource)
    return license


@pytest.fixture
async def resources_env():
    engine = await _engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        owner = await _pro(session, "owner@x.com")
        other = await _pro(session, "other@x.com")
        staff = await _pro(session, "staff@x.com", is_staff=True)
        global_item = await _resource(session, owner=None, title="Global")
        personal_item = await _resource(session, owner=owner, title="Pessoal")
        other_item = await _resource(session, owner=other, title="Outro")

        async def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "app.services.resource_service.storage_service.upload",
                    new_callable=AsyncMock,
                    return_value="resources/mock/file.pdf",
                ),
                patch(
                    "app.services.resource_service.storage_service.presigned_url",
                    new_callable=AsyncMock,
                    return_value="https://signed.example/file.pdf",
                ),
            ):
                yield {
                    "client": client,
                    "session": session,
                    "owner": owner,
                    "other": other,
                    "staff": staff,
                    "global_item": global_item,
                    "personal_item": personal_item,
                    "other_item": other_item,
                }
        app.dependency_overrides.clear()
    await engine.dispose()


def _headers(pro: Professional) -> dict[str, str]:
    token = create_access_token(pro.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_resources_scope(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]
    session = resources_env["session"]

    # Global sem licença/publicação não é público a outros profissionais (F17 conservador).
    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert all_res.status_code == 200
    titles = {item["title"] for item in all_res.json()}
    assert titles == {"Pessoal"}

    global_res = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(owner)
    )
    assert global_res.json() == []

    mine_res = await client.get(
        "/api/v1/resources?scope=mine", headers=_headers(owner)
    )
    assert {item["title"] for item in mine_res.json()} == {"Pessoal"}

    # Publicado + licença aprovada aparece para os demais (dono do global permanece None).
    await _publish_with_approved_license(session, resources_env["global_item"])

    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert {item["title"] for item in all_res.json()} == {"Global", "Pessoal"}

    global_res = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(owner)
    )
    global_titles = {item["title"] for item in global_res.json()}
    assert global_titles == {"Global"}
    global_payload = global_res.json()[0]
    assert global_payload["publicationStatus"] == "published"
    assert global_payload["license"]["status"] == "approved"
    assert global_payload["license"]["rightsHolder"] == "Titular dos direitos"
    assert global_payload["isMine"] is False

    # O próprio recurso publicado continua aparecendo em "meus", não em "globais".
    own_global = await client.get(
        "/api/v1/resources?scope=mine", headers=_headers(owner)
    )
    assert {item["title"] for item in own_global.json()} == {"Pessoal"}


@pytest.mark.asyncio
async def test_download_url_forbidden_for_other_personal(resources_env):
    client = resources_env["client"]
    other = resources_env["other"]
    other_item = resources_env["other_item"]

    res = await client.get(
        f"/api/v1/resources/{other_item.id}/download-url",
        headers=_headers(other),
    )
    assert res.status_code == 200

    owner = resources_env["owner"]
    blocked = await client.get(
        f"/api/v1/resources/{other_item.id}/download-url",
        headers=_headers(owner),
    )
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_shared_personal_not_public_until_approved_and_published(resources_env):
    """``sharedWithPlatform=true`` solicita revisão — não libera imediatamente."""
    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    other_item = resources_env["other_item"]

    other_item.shared_with_platform = True
    await session.commit()

    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert all_res.status_code == 200
    assert "Outro" not in {item["title"] for item in all_res.json()}

    global_res = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(owner)
    )
    assert "Outro" not in {item["title"] for item in global_res.json()}

    dl = await client.get(
        f"/api/v1/resources/{other_item.id}/download-url",
        headers=_headers(owner),
    )
    assert dl.status_code == 403

    # Só depois de licença aprovada + publicação o material entra no catálogo.
    await _publish_with_approved_license(session, other_item)

    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert "Outro" in {item["title"] for item in all_res.json()}

    global_res = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(owner)
    )
    assert "Outro" in {item["title"] for item in global_res.json()}

    dl = await client.get(
        f"/api/v1/resources/{other_item.id}/download-url",
        headers=_headers(owner),
    )
    assert dl.status_code == 200


@pytest.mark.asyncio
async def test_create_personal_resource_pdf(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    files = {"file": ("material.pdf", b"%PDF-1.4 test", "application/pdf")}
    data = {
        "title": "Meu PDF",
        "description": "Teste",
        "categories": '["Linguagem", "TEA"]',
        "ageRange": "3–5 anos",
        "relatedProtocol": "ABFW — Fonologia",
        "sharedWithPlatform": "true",
    }
    res = await client.post(
        "/api/v1/resources",
        headers=_headers(owner),
        data=data,
        files=files,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "Meu PDF"
    assert body["isMine"] is True
    assert body["sharedWithPlatform"] is True
    assert body["format"] == "PDF"
    # Nomes canônicos camelCase persistem no multipart.
    assert body["ageRange"] == "3–5 anos"
    assert body["relatedProtocol"] == "ABFW — Fonologia"
    # Compartilhar solicita revisão — não publica nem licencia automaticamente.
    assert body["publicationStatus"] == "draft"
    assert body["license"] is None
    assert body["canDeliverToFamily"] is False
    assert body["unavailableReason"]
    assert body["domainKeys"] == []
    assert len(body["contentSha256"]) == 64
    # Nada de storage_key/comprovação administrativa no DTO.
    assert "storageKey" not in body
    assert "evidenceReference" not in body


@pytest.mark.asyncio
async def test_create_accepts_legacy_snake_case_names(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    files = {"file": ("material.pdf", b"%PDF-1.4 legacy", "application/pdf")}
    data = {
        "title": "Legado",
        "categories": "[]",
        "age_range": "4 anos",
        "related_protocol": "MBGR",
        "shared_with_platform": "false",
    }
    res = await client.post(
        "/api/v1/resources", headers=_headers(owner), data=data, files=files
    )
    assert res.status_code == 201
    body = res.json()
    assert body["ageRange"] == "4 anos"
    assert body["relatedProtocol"] == "MBGR"
    assert body["sharedWithPlatform"] is False


@pytest.mark.asyncio
async def test_create_rejects_conflicting_alias_names(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    files = {"file": ("material.pdf", b"%PDF-1.4 alias", "application/pdf")}
    data = {
        "title": "Conflito",
        "categories": "[]",
        "ageRange": "3 anos",
        "age_range": "5 anos",
    }
    res = await client.post(
        "/api/v1/resources", headers=_headers(owner), data=data, files=files
    )
    assert res.status_code == 422
    assert "valores diferentes" in res.json()["detail"]

    # Mesmo valor nos dois nomes é aceito (compatibilidade temporária).
    data["age_range"] = "3 anos"
    res = await client.post(
        "/api/v1/resources", headers=_headers(owner), data=data, files=files
    )
    assert res.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_category",
    [
        "Fonoaudiologia",
        "Terapia Ocupacional",
        "Psicologia",
        "Fisioterapia",
        "Psicopedagogia",
    ],
)
async def test_create_resource_rejects_legacy_umbrella_category(
    resources_env, legacy_category
):
    client = resources_env["client"]
    owner = resources_env["owner"]

    files = {"file": ("material.pdf", b"%PDF-1.4 test", "application/pdf")}
    data = {
        "title": "Legado",
        "description": "Teste",
        "categories": f'["{legacy_category}"]',
    }
    res = await client.post(
        "/api/v1/resources",
        headers=_headers(owner),
        data=data,
        files=files,
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_unsupported_mime(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    files = {"file": ("bad.docx", b"data", "application/vnd.openxmlformats")}
    data = {"title": "DOCX", "categories": "[]"}
    res = await client.post(
        "/api/v1/resources",
        headers=_headers(owner),
        data=data,
        files=files,
    )
    assert res.status_code == 400


def test_validate_resource_upload_sanitizes_and_sniffs():
    from app.services.resource_service import validate_resource_upload

    ctype, fmt, name = validate_resource_upload(
        content_type="application/pdf",
        filename="../../etc/passwd.pdf",
        body=b"%PDF-1.4 safe",
    )
    assert ctype == "application/pdf"
    assert fmt == "PDF"
    assert name == "passwd.pdf"

    with pytest.raises(HTTPException) as exc:
        validate_resource_upload(
            content_type="image/svg+xml",
            filename="x.svg",
            body=b"<svg></svg>",
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        validate_resource_upload(
            content_type="image/png",
            filename="fake.png",
            body=b"%PDF-1.4\ndata",
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_personal_forbidden_on_global(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]
    global_item = resources_env["global_item"]

    res = await client.delete(
        f"/api/v1/resources/{global_item.id}",
        headers=_headers(owner),
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_family_portal_item_blocks_delete_and_replace(resources_env):
    """F14: item do portal (mesmo retirado) congela o hash e bloqueia o arquivo."""
    from app.services.resource_service import resource_has_references

    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    personal_item = resources_env["personal_item"]

    portal_item = FamilyPortalItem(
        portal_id=uuid.uuid4(),
        kind="material",
        status="draft",
        version=1,
        published_version=None,
        draft_content={"title": "Material do portal", "instructions": ""},
        draft_recipient_ids=[],
        draft_source_fingerprint=None,
        resource_id=personal_item.id,
        created_by_professional_id=owner.id,
    )
    session.add(portal_item)
    await session.commit()

    assert await resource_has_references(session, personal_item) is True

    blocked_delete = await client.delete(
        f"/api/v1/resources/{personal_item.id}", headers=_headers(owner)
    )
    assert blocked_delete.status_code == 409, blocked_delete.text
    blocked_replace = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"title": "Tentativa"},
        files={"file": ("novo.pdf", b"%PDF-1.4 novo", "application/pdf")},
    )
    assert blocked_replace.status_code == 409, blocked_replace.text

    # Retirado do portal continua bloqueando: a revisão congelou o hash.
    portal_item.status = "withdrawn"
    await session.commit()
    await session.refresh(portal_item)
    assert portal_item.status == "withdrawn"
    assert await resource_has_references(session, personal_item) is True
    still_blocked = await client.delete(
        f"/api/v1/resources/{personal_item.id}", headers=_headers(owner)
    )
    assert still_blocked.status_code == 409


@pytest.mark.asyncio
async def test_resource_file_streams_inline(resources_env):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    personal_item = resources_env["personal_item"]
    owner.email_verified_at = datetime.now(UTC)
    await session.commit()

    with patch(
        "app.services.resource_service.storage_service.download",
        new_callable=AsyncMock,
        return_value=(b"%PDF-1.7 test", "application/pdf"),
    ):
        res = await client.get(
            f"/api/v1/resources/{personal_item.id}/file",
            headers=_headers(owner),
        )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith('inline; filename="Pessoal"')
    assert res.content == b"%PDF-1.7 test"


@pytest.mark.asyncio
async def test_resource_file_hidden_for_unpublished_global(resources_env):
    """Global sem licença/publicação não é acessível a profissional comum."""
    client = resources_env["client"]
    owner = resources_env["owner"]
    global_item = resources_env["global_item"]

    res = await client.get(
        f"/api/v1/resources/{global_item.id}/file",
        headers=_headers(owner),
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_resource_file_forbidden_for_stranger_personal(resources_env):
    from datetime import UTC, datetime
    from unittest.mock import patch

    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    other_item = resources_env["other_item"]
    owner.email_verified_at = datetime.now(UTC)
    await session.commit()

    with patch(
        "app.services.resource_service.storage_service.download",
        new_callable=AsyncMock,
        return_value=(b"data", "application/pdf"),
    ):
        res = await client.get(
            f"/api/v1/resources/{other_item.id}/file",
            headers=_headers(owner),
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_create_global_resource(resources_env):
    client = resources_env["client"]
    staff = resources_env["staff"]

    files = {"file": ("catalog.pdf", b"%PDF-1.4 admin", "application/pdf")}
    data = {
        "title": "Novo global",
        "description": "Staff",
        "categories": '["Linguagem"]',
        "featured": "true",
        "ageRange": "0–6 anos",
    }
    res = await client.post(
        "/api/v1/admin/resources",
        headers=_headers(staff),
        data=data,
        files=files,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "Novo global"
    assert body["featured"] is True
    assert body["ageRange"] == "0–6 anos"
    # Material global criado pela curadoria nasce draft e sem licença.
    assert body["publicationStatus"] == "draft"
    assert body["license"] is None
    assert body["isMine"] is False


@pytest.mark.asyncio
async def test_admin_gate_blocks_non_staff(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    res = await client.get("/api/v1/admin/resources", headers=_headers(owner))
    assert res.status_code == 403


def test_seed_categories_are_valid():
    from app.core.resource_catalog import RESOURCE_CATEGORIES
    from app.seeds import resources_data

    assert resources_data.GLOBAL_RESOURCE_SEED, "seed deve ter recursos"
    for resource in resources_data.GLOBAL_RESOURCE_SEED:
        assert set(resource["categories"]) <= set(RESOURCE_CATEGORIES), (
            f"categorias inválidas em {resource['filename']}: {resource['categories']}"
        )


def test_tea_is_canonical_category():
    """Categoria TEA existe no catálogo canônico do fono (paridade com o web)."""
    from app.core.resource_catalog import RESOURCE_CATEGORIES

    assert "TEA" in RESOURCE_CATEGORIES


@pytest.mark.asyncio
async def test_patch_aliases_and_clear_fields(resources_env):
    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    personal_item = resources_env["personal_item"]

    personal_item.objective = "Ampliar vocabulário"
    personal_item.skill = "Nomeação"
    personal_item.age_range = "3–5 anos"
    personal_item.pages = 10
    await session.commit()

    # Nomes canônicos camelCase no PATCH.
    res = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"ageRange": "6 anos", "relatedProtocol": "PECS"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ageRange"] == "6 anos"
    assert body["relatedProtocol"] == "PECS"

    # Conflito entre nome novo e antigo -> 422.
    conflict = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"ageRange": "6 anos", "age_range": "7 anos"},
    )
    assert conflict.status_code == 422

    # clearFields limpa somente os campos listados (limpar != omitir).
    cleared = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"clearFields": '["objective", "pages", "ageRange"]'},
    )
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["objective"] is None
    assert body["pages"] is None
    assert body["ageRange"] is None
    assert body["skill"] == "Nomeação"

    # Valor vazio no mesmo campo limpo é aceito (string vazia não é valor).
    empty_ok = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"clearFields": '["skill"]', "skill": ""},
    )
    assert empty_ok.status_code == 200
    assert empty_ok.json()["skill"] is None

    # Limpar e definir valor na mesma requisição -> 422.
    contradiction = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"clearFields": '["objective"]', "objective": "Novo objetivo"},
    )
    assert contradiction.status_code == 422
    assert "não pode ser limpo" in contradiction.json()["detail"]

    # Campo não limpável -> 422; JSON inválido -> 400.
    unknown = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"clearFields": '["title"]'},
    )
    assert unknown.status_code == 422

    malformed = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"clearFields": "["},
    )
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_file_replacement_blocked_for_referenced_resource(resources_env):
    """Substituição no lugar só é permitida sem publicação/arquivo referenciado."""
    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    personal_item = resources_env["personal_item"]

    files = {"file": ("novo.pdf", b"%PDF-1.4 novo", "application/pdf")}
    res = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"title": "Pessoal"},
        files=files,
    )
    assert res.status_code == 200
    assert len(res.json()["contentSha256"]) == 64

    await _publish_with_approved_license(session, personal_item)

    blocked = await client.patch(
        f"/api/v1/resources/{personal_item.id}",
        headers=_headers(owner),
        data={"title": "Pessoal"},
        files=files,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Cadastre uma nova versão do material."


@pytest.mark.asyncio
async def test_list_pagination_ordering_and_domain_filter(resources_env):
    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]

    for index in range(3):
        await _resource(session, owner=owner, title=f"Extra {index}")

    collected: list[str] = []
    for offset in (0, 2, 4):
        res = await client.get(
            f"/api/v1/resources?scope=mine&limit=2&offset={offset}",
            headers=_headers(owner),
        )
        assert res.status_code == 200
        collected.extend(item["id"] for item in res.json())
    assert len(collected) == 4
    assert len(set(collected)) == 4, "paginação deve ser estável e sem repetição"

    too_big = await client.get("/api/v1/resources?limit=201", headers=_headers(owner))
    assert too_big.status_code == 422

    invalid_domain = await client.get(
        "/api/v1/resources?domainKey=inexistente", headers=_headers(owner)
    )
    assert invalid_domain.status_code == 422

    empty_domain = await client.get(
        "/api/v1/resources?domainKey=linguagem", headers=_headers(owner)
    )
    assert empty_domain.status_code == 200
    assert empty_domain.json() == []

    category = await client.get(
        "/api/v1/resources?scope=mine&category=TEA", headers=_headers(owner)
    )
    assert category.status_code == 200
    assert category.json() == []


@pytest.mark.asyncio
async def test_admin_list_filters_and_submissions(resources_env):
    client = resources_env["client"]
    session = resources_env["session"]
    staff = resources_env["staff"]
    global_item = resources_env["global_item"]
    personal_item = resources_env["personal_item"]
    other_item = resources_env["other_item"]

    default = await client.get("/api/v1/admin/resources", headers=_headers(staff))
    assert default.status_code == 200
    assert {item["title"] for item in default.json()} == {"Global"}

    # Submissão pessoal só entra com includeSubmissions e flag ligada.
    personal_item.shared_with_platform = True
    await session.commit()

    submitted = await client.get(
        "/api/v1/admin/resources?includeSubmissions=true", headers=_headers(staff)
    )
    assert {item["title"] for item in submitted.json()} == {"Global", "Pessoal"}

    published = await client.get(
        "/api/v1/admin/resources?publicationStatus=published", headers=_headers(staff)
    )
    assert published.json() == []

    await _publish_with_approved_license(session, global_item)

    published = await client.get(
        "/api/v1/admin/resources?publicationStatus=published", headers=_headers(staff)
    )
    assert {item["title"] for item in published.json()} == {"Global"}

    approved = await client.get(
        "/api/v1/admin/resources?licenseStatus=approved", headers=_headers(staff)
    )
    assert {item["title"] for item in approved.json()} == {"Global"}

    pending = await client.get(
        "/api/v1/admin/resources?licenseStatus=pending", headers=_headers(staff)
    )
    assert pending.json() == []
    assert other_item.shared_with_platform is False


@pytest.mark.asyncio
async def test_seed_skips_items_when_upload_fails(monkeypatch):
    from sqlalchemy import func, select

    from app.seeds.resources import seed_resources

    engine = await _engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:

            async def failing_upload(*_args, **_kwargs):
                raise RuntimeError("storage indisponível")

            monkeypatch.setattr("app.seeds.resources.storage_service.upload", failing_upload)
            await seed_resources(session)
            await session.commit()

            count = (
                await session.execute(select(func.count()).select_from(Resource))
            ).scalar_one()
            assert count == 0, "seed não persiste linha sem arquivo no storage"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_rows_are_draft_and_not_licensed(monkeypatch):
    from sqlalchemy import func, select

    from app.models.resource_license import ResourceLicense
    from app.seeds.resources import seed_resources

    engine = await _engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            upload = AsyncMock(return_value="resources/seed/item.pdf")
            monkeypatch.setattr("app.seeds.resources.storage_service.upload", upload)
            await seed_resources(session)
            await session.commit()

            rows = (await session.execute(select(Resource))).scalars().all()
            assert rows, "seed insere os materiais quando o upload funciona"
            assert all(row.publication_status == "draft" for row in rows)
            assert all(row.content_sha256 for row in rows)
            assert all(row.owner_professional_id is None for row in rows)

            licenses = (
                await session.execute(select(func.count()).select_from(ResourceLicense))
            ).scalar_one()
            assert licenses == 0, "seed demonstrativo não vira catálogo licenciado"
    finally:
        await engine.dispose()
