"""F17/4.2 — vínculos de recursos: domínios canônicos, metas e programas ABA.

Cobre: PUT de domínios (substituição idempotente, ordem canônica, ACL), filtro
``domainKey`` no catálogo pessoal e admin, vínculos de meta/programa idempotentes
por par de FKs, ACL que não expõe material privado de colega, gates de autor/ABA,
409 para material arquivado/licença retirada e a métrica de solicitações de
download (preview não conta; download-url incrementa por UPDATE atômico).
SQLite em memória com tabelas curadas; storage é sempre stub local.
"""

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.care_team import PatientCareTeamMember, PatientSharingConsentEvent
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.models.resource_link import (
    GoalResourceLink,
    ProgramResourceLink,
    ResourceDomainLink,
)
from app.services.resource_license_service import (
    ARCHIVED_REASON,
    LICENSE_STATUS_REASONS,
)
from app.services.resource_link_service import NO_ACCESS_REASON

DIGEST = "a" * 64


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                bind=sync_conn,
                tables=[
                    Professional.__table__,
                    Patient.__table__,
                    Goal.__table__,
                    InterventionProgram.__table__,
                    Resource.__table__,
                    ResourceLicense.__table__,
                    Base.metadata.tables["resource_license_decisions"],
                    ResourceDomainLink.__table__,
                    GoalResourceLink.__table__,
                    ProgramResourceLink.__table__,
                    PatientSharingConsentEvent.__table__,
                    PatientCareTeamMember.__table__,
                    FeatureFlag.__table__,
                    Base.metadata.tables["feature_flag_overrides"],
                    Base.metadata.tables["admin_audit_logs"],
                ],
            )
        )
    return engine


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


async def _patient(db: AsyncSession, owner: Professional, name: str = "Paciente") -> Patient:
    patient = Patient(
        professional_id=owner.id,
        name=name,
        birth_date=date(2020, 1, 1),
        start_date=date.today(),
        avatar_color="teal",
        diagnosis_keys=[],
    )
    db.add(patient)
    await db.commit()
    await db.refresh(patient)
    return patient


async def _goal(db: AsyncSession, patient: Patient, owner: Professional, title: str) -> Goal:
    goal = Goal(
        patient_id=patient.id,
        professional_id=owner.id,
        title=title,
        area="Linguagem",
        progress=10,
        start_date=date.today(),
        status="Inicial",
    )
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return goal


async def _program(
    db: AsyncSession, patient: Patient, owner: Professional, title: str = "Programa ABA"
) -> InterventionProgram:
    program = InterventionProgram(
        patient_id=patient.id,
        created_by_professional_id=owner.id,
        title=title,
        operational_definition="Definição operacional",
        teaching_strategy="Ensino incidental",
        mastery_percent=80,
        mastery_consecutive_sessions=2,
    )
    db.add(program)
    await db.commit()
    await db.refresh(program)
    return program


async def _resource(
    db: AsyncSession,
    *,
    owner: Professional | None,
    title: str,
    publication_status: str = "draft",
    license_status: str | None = None,
    allow_professional: bool = True,
) -> Resource:
    resource = Resource(
        owner_professional_id=owner.id if owner else None,
        title=title,
        description="Descrição",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=128,
        author=owner.name if owner else "Equipe KorusFono",
        storage_key=f"resources/test/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
        publication_status=publication_status,
    )
    db.add(resource)
    await db.commit()
    await db.refresh(resource)
    if license_status is not None:
        resource.content_sha256 = DIGEST
        db.add(
            ResourceLicense(
                resource_id=resource.id,
                version=1,
                status=license_status,
                origin="original",
                rights_holder="Titular",
                attribution="",
                allow_professional_distribution=allow_professional,
                allow_family_delivery=True,
                content_sha256=DIGEST,
            )
        )
        await db.commit()
        await db.refresh(resource)
    return resource


@pytest.fixture
async def env():
    engine = await _engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # ABA ligado: os gates existentes de equipe/programa dependem da flag.
        session.add(
            FeatureFlag(
                key="multidisciplinary_aba",
                description="Equipe multiprofissional e programas ABA",
                enabled_global=True,
            )
        )
        await session.commit()

        owner = await _pro(session, "owner@x.com", name="Dona da conta")
        other = await _pro(session, "other@x.com", name="Outra profissional")
        staff = await _pro(session, "staff@x.com", is_staff=True, name="Curadoria")
        support = await _pro(
            session, "support@x.com", is_staff=True, admin_role="support", name="Suporte"
        )
        practitioner = await _pro(session, "pract@x.com", name="Equipe clínica")

        patient = await _patient(session, owner, "Paciente da dona")
        patient2 = await _patient(session, owner, "Segundo paciente")
        goal = await _goal(session, patient, owner, "Meta da dona")
        goal2 = await _goal(session, patient2, owner, "Meta do outro paciente")
        program = await _program(session, patient, owner)
        program2 = await _program(session, patient2, owner, "Programa do outro paciente")

        consent = PatientSharingConsentEvent(
            patient_id=patient.id,
            decision="granted",
            policy_version="2026-09-11",
            recorded_by_professional_id=owner.id,
            recorded_at=datetime.now(UTC),
        )
        session.add(consent)
        await session.flush()
        session.add(
            PatientCareTeamMember(
                patient_id=patient.id,
                professional_id=practitioner.id,
                role="practitioner",
                status="active",
                invited_by_professional_id=owner.id,
                consent_event_id=consent.id,
                invited_at=datetime.now(UTC),
                accepted_at=datetime.now(UTC),
            )
        )
        await session.commit()

        own_resource = await _resource(session, owner=owner, title="Material da dona")
        other_private = await _resource(session, owner=other, title="Material da colega")
        other_published = await _resource(
            session,
            owner=other,
            title="Material publicado da colega",
            publication_status="published",
            license_status="approved",
        )
        global_published = await _resource(
            session,
            owner=None,
            title="Material da plataforma",
            publication_status="published",
            license_status="approved",
        )
        global_draft = await _resource(session, owner=None, title="Global em rascunho")
        archived_resource = await _resource(
            session,
            owner=owner,
            title="Material arquivado",
            publication_status="archived",
        )
        revoked_resource = await _resource(
            session, owner=owner, title="Material com licença retirada", license_status="revoked"
        )

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
                "practitioner": practitioner,
                "patient": patient,
                "patient2": patient2,
                "goal": goal,
                "goal2": goal2,
                "program": program,
                "program2": program2,
                "own_resource": own_resource,
                "other_private": other_private,
                "other_published": other_published,
                "global_published": global_published,
                "global_draft": global_draft,
                "archived_resource": archived_resource,
                "revoked_resource": revoked_resource,
            }
        app.dependency_overrides.clear()
    await engine.dispose()


def _headers(pro: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(pro.id)}"}


def _linked_keys(payload: dict) -> set[str]:
    return set(payload.keys())


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


# ---------------------------------------------------------------------------
# Domínios canônicos (PUT pessoal/admin + filtro do catálogo)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_domains_replaces_set_idempotently(env):
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    owner: Professional = env["owner"]
    resource: Resource = env["own_resource"]

    res = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["fala", "linguagem"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Ordem canônica do CLINICAL_DOMAIN_CATALOG, sem repetição.
    assert body["domainKeys"] == ["linguagem", "fala"]

    replay = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem", "fala"]},
    )
    assert replay.status_code == 200
    assert replay.json()["domainKeys"] == ["linguagem", "fala"]
    assert await _count(session, ResourceDomainLink) == 2

    replaced = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["social"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["domainKeys"] == ["social"]
    assert await _count(session, ResourceDomainLink) == 1

    cleared = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["domainKeys"] == []
    assert await _count(session, ResourceDomainLink) == 0

    # O catálogo rele o vínculo persistido.
    await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem"]},
    )
    listing = await client.get("/api/v1/resources?scope=mine", headers=_headers(owner))
    item = next(x for x in listing.json() if x["id"] == str(resource.id))
    assert item["domainKeys"] == ["linguagem"]


@pytest.mark.asyncio
async def test_put_domains_validates_catalog_uniqueness_and_acl(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    other: Professional = env["other"]
    resource: Resource = env["own_resource"]
    global_published: Resource = env["global_published"]

    invalid = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["inexistente"]},
    )
    assert invalid.status_code == 422
    assert "inválido" in invalid.json()["detail"]

    duplicated = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem", "linguagem"]},
    )
    assert duplicated.status_code == 422

    extra = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem"], "unexpected": True},
    )
    assert extra.status_code == 422

    # Pessoal só dono; material global não entra pela rota pessoal.
    foreign = await client.put(
        f"/api/v1/resources/{resource.id}/domains",
        headers=_headers(other),
        json={"domainKeys": ["linguagem"]},
    )
    assert foreign.status_code == 404
    global_via_personal = await client.put(
        f"/api/v1/resources/{global_published.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem"]},
    )
    assert global_via_personal.status_code == 404


@pytest.mark.asyncio
async def test_admin_domains_require_product_write_and_global_material(env):
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    staff: Professional = env["staff"]
    support: Professional = env["support"]
    owner: Professional = env["owner"]
    global_published: Resource = env["global_published"]
    own_resource: Resource = env["own_resource"]

    forbidden = await client.put(
        f"/api/v1/admin/resources/{global_published.id}/domains",
        headers=_headers(support),
        json={"domainKeys": ["linguagem"]},
    )
    assert forbidden.status_code == 403

    personal_via_admin = await client.put(
        f"/api/v1/admin/resources/{own_resource.id}/domains",
        headers=_headers(staff),
        json={"domainKeys": ["linguagem"]},
    )
    assert personal_via_admin.status_code == 404

    invalid = await client.put(
        f"/api/v1/admin/resources/{global_published.id}/domains",
        headers=_headers(staff),
        json={"domainKeys": ["inexistente"]},
    )
    assert invalid.status_code == 422

    ok = await client.put(
        f"/api/v1/admin/resources/{global_published.id}/domains",
        headers=_headers(staff),
        json={"domainKeys": ["atencao", "linguagem"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["domainKeys"] == ["linguagem", "atencao"]

    audit = (
        await session.execute(
            select(AdminAuditLog).where(AdminAuditLog.action == "update_resource_domains")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].actor_id == staff.id


@pytest.mark.asyncio
async def test_domain_filter_lists_only_linked_resources(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    staff: Professional = env["staff"]
    own_resource: Resource = env["own_resource"]
    global_published: Resource = env["global_published"]

    await client.put(
        f"/api/v1/resources/{own_resource.id}/domains",
        headers=_headers(owner),
        json={"domainKeys": ["linguagem"]},
    )
    await client.put(
        f"/api/v1/admin/resources/{global_published.id}/domains",
        headers=_headers(staff),
        json={"domainKeys": ["social"]},
    )

    personal = await client.get(
        "/api/v1/resources?domainKey=linguagem", headers=_headers(owner)
    )
    assert personal.status_code == 200
    assert {item["title"] for item in personal.json()} == {"Material da dona"}

    social = await client.get("/api/v1/resources?domainKey=social", headers=_headers(owner))
    assert {item["title"] for item in social.json()} == {"Material da plataforma"}

    admin_view = await client.get(
        "/api/v1/admin/resources?domainKey=social", headers=_headers(staff)
    )
    assert {item["title"] for item in admin_view.json()} == {"Material da plataforma"}

    invalid = await client.get(
        "/api/v1/admin/resources?domainKey=inexistente", headers=_headers(staff)
    )
    assert invalid.status_code == 422


# ---------------------------------------------------------------------------
# Vínculos de meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_resources_put_replay_and_delete_are_idempotent(env):
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    owner: Professional = env["owner"]
    patient: Patient = env["patient"]
    goal: Goal = env["goal"]
    own_resource: Resource = env["own_resource"]

    res = await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert _linked_keys(body) == {"resourceId", "title", "available", "reason", "resource"}
    assert body["resourceId"] == str(own_resource.id)
    assert body["title"] == "Material da dona"
    assert body["available"] is True
    assert body["reason"] is None
    assert body["resource"]["id"] == str(own_resource.id)

    replay = await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )
    assert replay.status_code == 200
    assert await _count(session, GoalResourceLink) == 1

    extra = await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={"unexpected": True},
    )
    assert extra.status_code == 422

    listing = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources", headers=_headers(owner)
    )
    assert listing.status_code == 200
    assert [item["resourceId"] for item in listing.json()] == [str(own_resource.id)]

    removed = await client.delete(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
    )
    assert removed.status_code == 204
    again = await client.delete(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
    )
    assert again.status_code == 204
    assert await _count(session, GoalResourceLink) == 0

    listing = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources", headers=_headers(owner)
    )
    assert listing.json() == []


@pytest.mark.asyncio
async def test_goal_resource_gates_require_author_and_patient_scope(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    other: Professional = env["other"]
    practitioner: Professional = env["practitioner"]
    patient: Patient = env["patient"]
    patient2: Patient = env["patient2"]
    goal: Goal = env["goal"]
    goal2: Goal = env["goal2"]
    own_resource: Resource = env["own_resource"]

    # Colega sem acesso clínico ao paciente: 404 em toda a linha.
    def _kwargs(method: str) -> dict:
        return {"json": {}} if method == "put" else {}

    for method, path in (
        ("get", f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources"),
        ("put", f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}"),
        ("delete", f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}"),
    ):
        res = await getattr(client, method)(path, headers=_headers(other), **_kwargs(method))
        assert res.status_code == 404

    # Membro da equipe lê (clinical:read), mas não gerencia: autor da meta.
    await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )
    read = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources",
        headers=_headers(practitioner),
    )
    assert read.status_code == 200
    blocked_put = await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(practitioner),
        json={},
    )
    assert blocked_put.status_code == 404
    blocked_delete = await client.delete(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(practitioner),
    )
    assert blocked_delete.status_code == 404

    # Meta de outro paciente / inexistente: 404.
    wrong_patient = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal2.id}/resources", headers=_headers(owner)
    )
    assert wrong_patient.status_code == 404
    unknown_goal = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{uuid.uuid4()}/resources", headers=_headers(owner)
    )
    assert unknown_goal.status_code == 404
    unknown_resource = await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{uuid.uuid4()}",
        headers=_headers(owner),
        json={},
    )
    assert unknown_resource.status_code == 404


@pytest.mark.asyncio
async def test_goal_link_hides_private_colleague_resource_without_acl(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    practitioner: Professional = env["practitioner"]
    patient: Patient = env["patient"]
    goal: Goal = env["goal"]
    own_resource: Resource = env["own_resource"]

    await client.put(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )

    # Dono vê o próprio material com título e DTO completo.
    owner_view = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources", headers=_headers(owner)
    )
    owner_item = owner_view.json()[0]
    assert owner_item["available"] is True
    assert owner_item["title"] == "Material da dona"
    assert owner_item["resource"]["isMine"] is True

    # Colega da equipe sem ACL: indisponível, sem título clínico/arquivo.
    team_view = await client.get(
        f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources",
        headers=_headers(practitioner),
    )
    team_item = team_view.json()[0]
    assert team_item["resourceId"] == str(own_resource.id)
    assert team_item["available"] is False
    assert team_item["reason"] == NO_ACCESS_REASON
    assert team_item["title"] == "Material de outro profissional"
    assert team_item["resource"] is None
    assert "Material da dona" not in team_view.text


@pytest.mark.asyncio
async def test_goal_link_accepts_acl_material_and_rejects_unavailable_ones(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    patient: Patient = env["patient"]
    goal: Goal = env["goal"]
    other_private: Resource = env["other_private"]
    other_published: Resource = env["other_published"]
    archived_resource: Resource = env["archived_resource"]
    revoked_resource: Resource = env["revoked_resource"]
    global_draft: Resource = env["global_draft"]

    def url(resource: Resource) -> str:
        return f"/api/v1/patients/{patient.id}/goals/{goal.id}/resources/{resource.id}"

    # Material privado de colega nunca entra por vínculo.
    forbidden = await client.put(url(other_private), headers=_headers(owner), json={})
    assert forbidden.status_code == 403

    # Publicado + licença aprovada vale para toda a plataforma (ACL do catálogo).
    allowed = await client.put(url(other_published), headers=_headers(owner), json={})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["available"] is True

    # Arquivado / licença retirada / global sem licença vigente: 409.
    archived = await client.put(url(archived_resource), headers=_headers(owner), json={})
    assert archived.status_code == 409
    assert archived.json()["detail"] == ARCHIVED_REASON

    revoked = await client.put(url(revoked_resource), headers=_headers(owner), json={})
    assert revoked.status_code == 409
    assert revoked.json()["detail"] == LICENSE_STATUS_REASONS["revoked"]

    draft = await client.put(url(global_draft), headers=_headers(owner), json={})
    assert draft.status_code == 409


# ---------------------------------------------------------------------------
# Vínculos de programa ABA (gate existente)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_program_resources_follow_aba_gate_and_roles(env):
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    owner: Professional = env["owner"]
    other: Professional = env["other"]
    practitioner: Professional = env["practitioner"]
    patient: Patient = env["patient"]
    patient2: Patient = env["patient2"]
    program: InterventionProgram = env["program"]
    program2: InterventionProgram = env["program2"]
    own_resource: Resource = env["own_resource"]

    # Flag ABA desligada bloqueia até o dono (gate existente).
    flag = await session.get(FeatureFlag, "multidisciplinary_aba")
    flag.enabled_global = False
    await session.commit()
    off = await client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources",
        headers=_headers(owner),
    )
    assert off.status_code == 404

    flag.enabled_global = True
    await session.commit()

    put = await client.put(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )
    assert put.status_code == 200, put.text
    replay = await client.put(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources/{own_resource.id}",
        headers=_headers(owner),
        json={},
    )
    assert replay.status_code == 200
    assert await _count(session, ProgramResourceLink) == 1

    # Membro practitioner não tem program:manage — gate existente devolve 404.
    blocked = await client.put(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources/{own_resource.id}",
        headers=_headers(practitioner),
        json={},
    )
    assert blocked.status_code == 404
    readable = await client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources",
        headers=_headers(practitioner),
    )
    assert readable.status_code == 200
    assert readable.json()[0]["resource"] is None  # sem ACL no material privado do dono

    # Fora do escopo/programa de outro paciente: 404.
    scope = await client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program2.id}/resources",
        headers=_headers(owner),
    )
    assert scope.status_code == 404
    foreign = await client.get(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources",
        headers=_headers(other),
    )
    assert foreign.status_code == 404

    removed = await client.delete(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources/{own_resource.id}",
        headers=_headers(owner),
    )
    assert removed.status_code == 204
    again = await client.delete(
        f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}/resources/{own_resource.id}",
        headers=_headers(owner),
    )
    assert again.status_code == 204
    assert await _count(session, ProgramResourceLink) == 0


@pytest.mark.asyncio
async def test_program_link_uses_same_resource_acl(env):
    client: AsyncClient = env["client"]
    owner: Professional = env["owner"]
    patient: Patient = env["patient"]
    program: InterventionProgram = env["program"]
    other_private: Resource = env["other_private"]
    archived_resource: Resource = env["archived_resource"]

    def url(resource: Resource) -> str:
        return (
            f"/api/v1/patients/{patient.id}/intervention-programs/{program.id}"
            f"/resources/{resource.id}"
        )

    forbidden = await client.put(url(other_private), headers=_headers(owner), json={})
    assert forbidden.status_code == 403
    archived = await client.put(url(archived_resource), headers=_headers(owner), json={})
    assert archived.status_code == 409


# ---------------------------------------------------------------------------
# Métrica de downloads (pendências da 4.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_does_not_increment_downloads_and_download_url_does(env):
    client: AsyncClient = env["client"]
    session: AsyncSession = env["session"]
    owner: Professional = env["owner"]
    resource: Resource = env["own_resource"]

    with (
        patch(
            "app.services.resource_service.storage_service.download",
            new_callable=AsyncMock,
            return_value=(b"%PDF-1.7 preview", "application/pdf"),
        ),
        patch(
            "app.services.resource_service.storage_service.presigned_url",
            new_callable=AsyncMock,
            return_value="https://signed.example/file.pdf",
        ),
    ):
        preview = await client.get(
            f"/api/v1/resources/{resource.id}/file", headers=_headers(owner)
        )
        assert preview.status_code == 200
        await session.refresh(resource)
        assert resource.downloads == 0, "preview não conta solicitação de download"

        first = await client.get(
            f"/api/v1/resources/{resource.id}/download-url", headers=_headers(owner)
        )
        assert first.status_code == 200
        assert first.json()["url"] == "https://signed.example/file.pdf"
        await session.refresh(resource)
        assert resource.downloads == 1

        await client.get(f"/api/v1/resources/{resource.id}/file", headers=_headers(owner))
        await client.get(
            f"/api/v1/resources/{resource.id}/download-url", headers=_headers(owner)
        )
        await session.refresh(resource)
        assert resource.downloads == 2
