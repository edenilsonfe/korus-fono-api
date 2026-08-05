"""Resources library — ownership, mime validation, admin gate."""

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
from app.models.professional import Professional
from app.models.resource import Resource

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
                tables=[Professional.__table__, Resource.__table__],
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

    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert all_res.status_code == 200
    titles = {item["title"] for item in all_res.json()}
    assert titles == {"Global", "Pessoal"}

    global_res = await client.get(
        "/api/v1/resources?scope=global", headers=_headers(owner)
    )
    assert {item["title"] for item in global_res.json()} == {"Global"}

    mine_res = await client.get(
        "/api/v1/resources?scope=mine", headers=_headers(owner)
    )
    assert {item["title"] for item in mine_res.json()} == {"Pessoal"}


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
async def test_shared_personal_visible_to_others(resources_env):
    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    other_item = resources_env["other_item"]

    other_item.shared_with_platform = True
    await session.commit()

    all_res = await client.get("/api/v1/resources", headers=_headers(owner))
    assert all_res.status_code == 200
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
        "categories": '["Linguagem"]',
        "shared_with_platform": "true",
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
async def test_resource_file_streams_inline(resources_env):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    client = resources_env["client"]
    session = resources_env["session"]
    owner = resources_env["owner"]
    global_item = resources_env["global_item"]
    owner.email_verified_at = datetime.now(UTC)
    await session.commit()

    with patch(
        "app.services.resource_service.storage_service.download",
        new_callable=AsyncMock,
        return_value=(b"%PDF-1.7 test", "application/pdf"),
    ):
        res = await client.get(
            f"/api/v1/resources/{global_item.id}/file",
            headers=_headers(owner),
        )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith('inline; filename="Global"')
    assert res.content == b"%PDF-1.7 test"


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
        "categories": '["TEA"]',
        "featured": "true",
    }
    res = await client.post(
        "/api/v1/admin/resources",
        headers=_headers(staff),
        data=data,
        files=files,
    )
    assert res.status_code == 201
    assert res.json()["title"] == "Novo global"
    assert res.json()["featured"] is True


@pytest.mark.asyncio
async def test_admin_gate_blocks_non_staff(resources_env):
    client = resources_env["client"]
    owner = resources_env["owner"]

    res = await client.get("/api/v1/admin/resources", headers=_headers(owner))
    assert res.status_code == 403
