"""F16 — programa de casa: prescrição, publicação e consulta (Tarefa 5.1).

Cobre: draft comum sem flag ABA, XOR meta/programa ABA, limites/validações,
controle otimista, definição publicada imutável, republicação idempotente,
revalidação de licença familiar F17, arquivamento com revogação de grants,
leitura clínica compartilhada e escopo do dono. SQLite em memória.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import UniqueConstraint, func, select

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.models.care_team import (
    PatientCareTeamMember,
    PatientSharingConsentEvent,
)
from app.models.caregiver import Caregiver
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgram,
    HomeProgramGrant,
    HomeProgramTask,
    HomeProgramTaskResource,
)
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense

START = date(2026, 9, 10)
END = START + timedelta(days=13)


def _headers(professional: Professional) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {create_access_token(professional.id, professional.token_version)}"
        )
    }


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


async def _other_patient(db_session, *, owner: Professional) -> Patient:
    patient = Patient(
        professional_id=owner.id,
        name="Paciente de outra",
        birth_date=date(2019, 3, 3),
        diagnosis_keys=[],
        status="ativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


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


async def _goal(
    db_session, patient: Patient, professional: Professional, *, title: str = "Nomear animais"
) -> Goal:
    goal = Goal(
        patient_id=patient.id,
        professional_id=professional.id,
        title=title,
        area="Linguagem",
        start_date=START,
        status="Em andamento",
    )
    db_session.add(goal)
    await db_session.commit()
    await db_session.refresh(goal)
    return goal


async def _aba_program(
    db_session,
    patient: Patient,
    professional: Professional,
    *,
    status: str = "active",
) -> InterventionProgram:
    program = InterventionProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Ensino de tato",
        operational_definition="Nomeia figuras de animais em Português.",
        teaching_strategy="Ensino por tentativas discretas com reforço.",
        mastery_percent=80,
        mastery_consecutive_sessions=3,
        status=status,
    )
    db_session.add(program)
    await db_session.commit()
    await db_session.refresh(program)
    return program


async def _resource(
    db_session,
    professional: Professional,
    *,
    title: str = "Cartões de animais",
    family: bool = True,
    license_status: str = "declared",
    sha: str = "a" * 64,
    with_license: bool = True,
) -> tuple[Resource, ResourceLicense | None]:
    resource = Resource(
        owner_professional_id=professional.id,
        title=title,
        description="",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=100,
        author=professional.name,
        storage_key=f"resources/test/{uuid4().hex}.pdf",
        content_type="application/pdf",
        content_sha256=sha,
    )
    db_session.add(resource)
    await db_session.flush()
    license = None
    if with_license:
        license = ResourceLicense(
            resource_id=resource.id,
            version=1,
            status=license_status,
            origin="original",
            rights_holder=professional.name,
            attribution="Uso autorizado pela autora",
            allow_professional_distribution=True,
            allow_family_delivery=family,
            content_sha256=sha,
        )
        db_session.add(license)
    await db_session.commit()
    await db_session.refresh(resource)
    if license is not None:
        await db_session.refresh(license)
    return resource, license


def _task(*, goal_id: UUID | None = None, aba_id: UUID | None = None, **overrides) -> dict:
    task = {
        "title": "Nomear figuras",
        "instructions": "Mostre os cartões e peça o nome.",
        "dueOn": START.isoformat(),
        "goalId": str(goal_id) if goal_id else None,
        "interventionProgramId": str(aba_id) if aba_id else None,
        "resourceIds": [],
    }
    task.update(overrides)
    return task


def _body(*, goal_id: UUID | None = None, aba_id: UUID | None = None, tasks=None, **overrides) -> dict:
    body = {
        "title": "Programa de casa",
        "startsOn": START.isoformat(),
        "endsOn": END.isoformat(),
        "tasks": tasks or [_task(goal_id=goal_id, aba_id=aba_id)],
    }
    body.update(overrides)
    return body


async def _consent(api_client, auth_headers, patient: Patient, decision: str = "granted") -> dict:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": decision, "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_program(api_client, auth_headers, patient: Patient, body: dict) -> dict:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Criação (draft)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Contrato de persistência (base para a migration M5)
# ---------------------------------------------------------------------------


def test_home_program_tables_and_constraints_are_complete():
    """As 8 tabelas da F16 (M5) existem com as restrições estruturais do §3.5."""
    tables = Base.metadata.tables
    expected = {
        "home_programs",
        "home_program_tasks",
        "home_program_task_resources",
        "home_program_grants",
        "home_program_check_ins",
        "home_program_check_in_revisions",
        "home_program_photos",
        "home_program_events",
    }
    assert expected <= set(tables)

    task_constraints = {c.name for c in tables["home_program_tasks"].constraints}
    assert "ck_home_program_task_target_xor" in task_constraints
    assert "uq_home_program_task_client_id" in task_constraints

    def _unique_sets(table_name: str) -> set[frozenset]:
        return {
            frozenset(column.name for column in constraint.columns)
            for constraint in tables[table_name].constraints
            if isinstance(constraint, UniqueConstraint)
        }

    assert frozenset({"task_id"}) in _unique_sets("home_program_check_ins")
    assert frozenset({"grant_id", "client_record_id"}) in _unique_sets(
        "home_program_events"
    )
    assert frozenset({"program_id", "client_task_id"}) in _unique_sets(
        "home_program_tasks"
    )
    assert frozenset({"task_id", "resource_id"}) in _unique_sets(
        "home_program_task_resources"
    )

    grant_indexes = {index.name for index in tables["home_program_grants"].indexes}
    assert "uq_home_program_grants_active" in grant_indexes
    photo_indexes = {index.name for index in tables["home_program_photos"].indexes}
    assert "uq_home_program_photos_current" in photo_indexes

    grant_columns = tables["home_program_grants"].columns
    assert grant_columns["caregiver_id"].nullable is True
    assert {
        "caregiver_name_snapshot",
        "caregiver_relation_snapshot",
        "token_hash",
        "expires_at",
        "revoked_at",
        "consent_event_id",
        "family_authorization",
    } <= set(grant_columns.keys())


async def test_create_draft_with_common_goal_works_without_aba_flag(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    client_id = str(uuid4())

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=_body(goal_id=goal.id, tasks=[_task(goal_id=goal.id, id=client_id)]),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "draft"
    assert data["version"] == 1
    assert data["patientId"] == str(patient.id)
    assert data["title"] == "Programa de casa"
    assert data["startsOn"] == START.isoformat()
    assert data["endsOn"] == END.isoformat()
    assert data["timezone"] == "America/Sao_Paulo"
    task = data["tasks"][0]
    assert task["goalId"] == str(goal.id)
    assert task["interventionProgramId"] is None
    assert task["clientTaskId"] == client_id
    assert task["resources"] == []

    program = await db_session.scalar(
        select(HomeProgram).where(HomeProgram.id == UUID(data["id"]))
    )
    await db_session.refresh(program)
    assert program.status == "draft"
    assert program.version == 1
    assert program.created_by_professional_id == professional.id
    row = await db_session.scalar(
        select(HomeProgramTask).where(HomeProgramTask.program_id == program.id)
    )
    assert row is not None
    assert row.client_task_id == UUID(client_id)
    assert row.goal_id == goal.id


async def test_create_rejects_invalid_payloads_with_422(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    url = f"/api/v1/patients/{patient.id}/home-programs"

    async def post(body):
        return await api_client.post(url, headers=auth_headers, json=body)

    # período maior que 30 dias
    over = _body(goal_id=goal.id)
    over["endsOn"] = (START + timedelta(days=31)).isoformat()
    assert (await post(over)).status_code == 422

    # fim antes do início
    inverted = _body(goal_id=goal.id)
    inverted["startsOn"], inverted["endsOn"] = END.isoformat(), START.isoformat()
    assert (await post(inverted)).status_code == 422

    # dueOn fora do período (antes do início e depois do fim)
    before = _body(
        goal_id=goal.id,
        tasks=[_task(goal_id=goal.id, dueOn=(START - timedelta(days=1)).isoformat())],
    )
    assert (await post(before)).status_code == 422
    after = _body(
        goal_id=goal.id,
        tasks=[_task(goal_id=goal.id, dueOn=(END + timedelta(days=1)).isoformat())],
    )
    assert (await post(after)).status_code == 422

    # XOR: meta e programa ABA juntos / nenhum dos dois
    both = _body(tasks=[_task(goal_id=goal.id, aba_id=uuid4())])
    assert (await post(both)).status_code == 422
    neither = _body(tasks=[_task(goal_id=None, aba_id=None)])
    assert (await post(neither)).status_code == 422

    # client ids duplicados
    duplicated = str(uuid4())
    twice = _body(
        tasks=[
            _task(goal_id=goal.id, id=duplicated),
            _task(goal_id=goal.id, id=duplicated),
        ]
    )
    assert (await post(twice)).status_code == 422

    # mais de 50 tarefas
    many = _body(
        tasks=[_task(goal_id=goal.id, id=str(uuid4())) for _ in range(51)]
    )
    assert (await post(many)).status_code == 422

    # campo desconhecido no payload (extra=forbid)
    unknown = _body(goal_id=goal.id)
    unknown["unexpected"] = True
    assert (await post(unknown)).status_code == 422

    # mais de 5 recursos por tarefa
    six = _body(
        tasks=[
            _task(
                goal_id=goal.id,
                resourceIds=[str(uuid4()) for _ in range(6)],
            )
        ]
    )
    assert (await post(six)).status_code == 422

    # título/comentário em branco
    blank = _body(
        goal_id=goal.id,
        tasks=[_task(goal_id=goal.id, title="   ")],
    )
    assert (await post(blank)).status_code == 422

    # nada foi persistido
    assert await db_session.scalar(select(func.count()).select_from(HomeProgram)) == 0


async def test_create_rejects_foreign_scope_with_404(
    api_client, auth_headers, db_session, patient, professional
):
    other = await _other_professional(db_session)
    foreign_patient = await _other_patient(db_session, owner=other)

    # paciente de outra profissional
    response = await api_client.post(
        f"/api/v1/patients/{foreign_patient.id}/home-programs",
        headers=auth_headers,
        json=_body(goal_id=uuid4()),
    )
    assert response.status_code == 404

    # meta fora do paciente
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=_body(goal_id=uuid4()),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Meta não encontrada"

    # programa ABA fora do paciente / inexistente
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json=_body(aba_id=uuid4()),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Programa ABA não encontrado"


# ---------------------------------------------------------------------------
# PATCH (draft, controle otimista)
# ---------------------------------------------------------------------------


async def test_patch_optimistic_control_and_published_immutability(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    data = await _create_program(
        api_client, auth_headers, patient, _body(goal_id=goal.id)
    )
    url = f"/api/v1/patients/{patient.id}/home-programs/{data['id']}"

    # versão obsoleta
    stale = await api_client.patch(
        url, headers=auth_headers, json={"expectedVersion": 5, "title": "Nova"}
    )
    assert stale.status_code == 409

    # sem alteração efetiva
    empty = await api_client.patch(
        url, headers=auth_headers, json={"expectedVersion": 1}
    )
    assert empty.status_code == 422

    # alteração válida: título + tarefas substituídas
    new_client = str(uuid4())
    updated = await api_client.patch(
        url,
        headers=auth_headers,
        json={
            "expectedVersion": 1,
            "title": "Rotina atualizada",
            "tasks": [
                _task(
                    goal_id=goal.id,
                    id=new_client,
                    dueOn=(START + timedelta(days=1)).isoformat(),
                )
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["version"] == 2
    assert body["title"] == "Rotina atualizada"
    assert body["tasks"][0]["clientTaskId"] == new_client
    count = await db_session.scalar(
        select(func.count())
        .select_from(HomeProgramTask)
        .where(HomeProgramTask.program_id == UUID(data["id"]))
    )
    assert count == 1

    # publica e congela a definição
    published = await api_client.post(
        url + "/publish", headers=auth_headers, json={"expectedVersion": 2}
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "active"
    assert published.json()["version"] == 3

    frozen = await api_client.patch(
        url, headers=auth_headers, json={"expectedVersion": 3, "title": "Proibido"}
    )
    assert frozen.status_code == 409
    assert "imutável" in frozen.json()["detail"]

    # outra profissional não enxerga nem altera
    other = await _other_professional(db_session, email="colega@example.com")
    response = await api_client.patch(
        url, headers=_headers(other), json={"expectedVersion": 3, "title": "X"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Publicação / arquivamento
# ---------------------------------------------------------------------------


async def test_publish_revalidates_and_is_idempotent(
    api_client, auth_headers, db_session, patient, professional
):
    resource, license_row = await _resource(db_session, professional)
    goal = await _goal(db_session, patient, professional)
    data = await _create_program(
        api_client,
        auth_headers,
        patient,
        _body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(resource.id)])]
        ),
    )
    url = f"/api/v1/patients/{patient.id}/home-programs/{data['id']}"

    first = await api_client.post(
        url + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "active"
    assert first.json()["version"] == 2

    # idempotência: mesmo estado não republica
    again = await api_client.post(
        url + "/publish", headers=auth_headers, json={"expectedVersion": 2}
    )
    assert again.status_code == 200
    assert again.json()["version"] == 2

    # versão obsoleta
    stale = await api_client.post(
        url + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    assert stale.status_code == 409

    # revalidação: licença retirada impede publicar o segundo rascunho
    second = await _create_program(
        api_client,
        auth_headers,
        patient,
        _body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(resource.id)])]
        ),
    )
    second_url = f"/api/v1/patients/{patient.id}/home-programs/{second['id']}"
    license_row.status = "revoked"
    await db_session.commit()
    blocked = await api_client.post(
        second_url + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    assert blocked.status_code == 409

    license_row.status = "declared"
    await db_session.commit()
    recovered = await api_client.post(
        second_url + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    assert recovered.status_code == 200, recovered.text

    # arquivado não é republicado
    archived = await api_client.post(second_url + "/archive", headers=auth_headers)
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    republish = await api_client.post(
        second_url + "/publish", headers=auth_headers, json={"expectedVersion": 3}
    )
    assert republish.status_code == 409
    assert "arquivado" in republish.json()["detail"]

    # arquivamento é idempotente
    archived_again = await api_client.post(second_url + "/archive", headers=auth_headers)
    assert archived_again.status_code == 200
    assert archived_again.json()["status"] == "archived"


async def test_archive_revokes_active_grants(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    data = await _create_program(
        api_client, auth_headers, patient, _body(goal_id=goal.id)
    )
    url = f"/api/v1/patients/{patient.id}/home-programs/{data['id']}"
    await api_client.post(
        url + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    grant = await api_client.post(
        url + "/grants",
        headers=auth_headers,
        json={
            "caregiverId": str(caregiver.id),
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reviewed": True,
            },
        },
    )
    assert grant.status_code == 201, grant.text

    archived = await api_client.post(url + "/archive", headers=auth_headers, json={})
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    row = await db_session.scalar(
        select(HomeProgramGrant).where(
            HomeProgramGrant.id == UUID(grant.json()["id"])
        )
    )
    await db_session.refresh(row)
    assert row.revoked_at is not None


# ---------------------------------------------------------------------------
# Gate ABA e materiais familiares
# ---------------------------------------------------------------------------


async def test_aba_link_requires_flag_active_program_and_consent(
    api_client, auth_headers, db_session, patient, professional
):
    aba_active = await _aba_program(db_session, patient, professional)
    aba_draft = await _aba_program(
        db_session, patient, professional, status="draft"
    )
    url = f"/api/v1/patients/{patient.id}/home-programs"

    # flag desligada (default-off) bloqueia o vínculo ABA
    off = await api_client.post(
        url, headers=auth_headers, json=_body(aba_id=aba_active.id)
    )
    assert off.status_code == 409

    await _enable_aba_flag(db_session)

    # programa ABA em rascunho não é vinculável
    draft = await api_client.post(
        url, headers=auth_headers, json=_body(aba_id=aba_draft.id)
    )
    assert draft.status_code == 409
    assert "ativo" in draft.json()["detail"]

    # sem autorização assistencial vigente
    no_consent = await api_client.post(
        url, headers=auth_headers, json=_body(aba_id=aba_active.id)
    )
    assert no_consent.status_code == 409
    assert "autorização" in no_consent.json()["detail"].lower()

    # com consentimento registrado funciona
    await _consent(api_client, auth_headers, patient)
    ok = await api_client.post(
        url, headers=auth_headers, json=_body(aba_id=aba_active.id)
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["tasks"][0]["interventionProgramId"] == str(aba_active.id)

    # retirada: nova tentativa de criação falha
    await _consent(api_client, auth_headers, patient, decision="withdrawn")
    after = await api_client.post(
        url, headers=auth_headers, json=_body(aba_id=aba_active.id)
    )
    assert after.status_code == 409


async def test_resources_require_family_license_and_freeze_snapshot(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    url = f"/api/v1/patients/{patient.id}/home-programs"

    no_license, _ = await _resource(
        db_session, professional, title="Sem licença", with_license=False
    )
    denied = await api_client.post(
        url,
        headers=auth_headers,
        json=_body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(no_license.id)])]
        ),
    )
    assert denied.status_code == 409

    no_family, _ = await _resource(
        db_session, professional, title="Sem família", family=False
    )
    denied_family = await api_client.post(
        url,
        headers=auth_headers,
        json=_body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(no_family.id)])]
        ),
    )
    assert denied_family.status_code == 409
    assert "família" in denied_family.json()["detail"]

    shared, license_row = await _resource(
        db_session, professional, title="Cartões aprovados"
    )
    created = await api_client.post(
        url,
        headers=auth_headers,
        json=_body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(shared.id)])]
        ),
    )
    assert created.status_code == 201, created.text
    item = created.json()["tasks"][0]["resources"][0]
    assert item["resourceId"] == str(shared.id)
    assert item["title"] == "Cartões aprovados"
    assert item["available"] is True
    assert item["reason"] is None

    link = await db_session.scalar(
        select(HomeProgramTaskResource).where(
            HomeProgramTaskResource.resource_id == shared.id
        )
    )
    assert link is not None
    assert link.resource_sha256 == shared.content_sha256
    assert link.license_id == license_row.id
    assert link.license_version == 1

    # arquivado bloqueia nova prescrição
    shared.publication_status = "archived"
    await db_session.commit()
    archived = await api_client.post(
        url,
        headers=auth_headers,
        json=_body(
            tasks=[_task(goal_id=goal.id, resourceIds=[str(shared.id)])]
        ),
    )
    assert archived.status_code == 409


# ---------------------------------------------------------------------------
# Consulta e acesso clínico
# ---------------------------------------------------------------------------


async def test_list_get_and_scope(
    api_client, auth_headers, db_session, patient, professional
):
    goal = await _goal(db_session, patient, professional)
    first = await _create_program(
        api_client, auth_headers, patient, _body(goal_id=goal.id)
    )
    await _create_program(api_client, auth_headers, patient, _body(goal_id=goal.id))
    base = f"/api/v1/patients/{patient.id}/home-programs"

    listing = await api_client.get(base, headers=auth_headers)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["limit"] == 20
    assert len(payload["items"]) == 2

    over_limit = await api_client.get(base + "?limit=101", headers=auth_headers)
    assert over_limit.status_code == 422

    detail = await api_client.get(base + f"/{first['id']}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]

    missing = await api_client.get(base + f"/{uuid4()}", headers=auth_headers)
    assert missing.status_code == 404

    other = await _other_professional(db_session, email="sem-acesso@example.com")
    denied = await api_client.get(base, headers=_headers(other))
    assert denied.status_code == 404


async def test_shared_clinical_reader_sees_tracking_but_cannot_prescribe(
    api_client, auth_headers, db_session, patient, professional
):
    await _enable_aba_flag(db_session)
    consent = await _consent(api_client, auth_headers, patient)
    goal = await _goal(db_session, patient, professional)
    data = await _create_program(
        api_client, auth_headers, patient, _body(goal_id=goal.id)
    )
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

    base = f"/api/v1/patients/{patient.id}/home-programs"
    invited_headers = _headers(invited)

    listing = await api_client.get(base, headers=invited_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    detail = await api_client.get(base + f"/{data['id']}", headers=invited_headers)
    assert detail.status_code == 200
    # leitura clínica sem contatos do responsável
    assert "Maria Silva" not in detail.text

    create = await api_client.post(
        base, headers=invited_headers, json=_body(goal_id=goal.id)
    )
    assert create.status_code == 404

    patch = await api_client.patch(
        base + f"/{data['id']}",
        headers=invited_headers,
        json={"expectedVersion": 1, "title": "tentativa"},
    )
    assert patch.status_code == 404

    publish = await api_client.post(
        base + f"/{data['id']}/publish",
        headers=invited_headers,
        json={"expectedVersion": 1},
    )
    assert publish.status_code == 404

    grants = await api_client.get(
        base + f"/{data['id']}/grants", headers=invited_headers
    )
    assert grants.status_code == 404

    # com a flag desligada, o membro também perde a leitura compartilhada
    flag = await db_session.get(FeatureFlag, "multidisciplinary_aba")
    flag.enabled_global = False
    await db_session.commit()
    blocked = await api_client.get(base, headers=invited_headers)
    assert blocked.status_code == 404


async def test_patient_sharing_consent_event_stored_on_grant_for_aba(
    api_client, auth_headers, db_session, patient, professional
):
    """O gate ABA grava o evento de consentimento usado no grant."""
    await _enable_aba_flag(db_session)
    consent = await _consent(api_client, auth_headers, patient)
    aba = await _aba_program(db_session, patient, professional)
    data = await _create_program(
        api_client, auth_headers, patient, _body(aba_id=aba.id)
    )
    base = f"/api/v1/patients/{patient.id}/home-programs/{data['id']}"
    await api_client.post(
        base + "/publish", headers=auth_headers, json={"expectedVersion": 1}
    )
    caregiver = await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )
    grant = await api_client.post(
        base + "/grants",
        headers=auth_headers,
        json={
            "caregiverId": str(caregiver.id),
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "reviewed": True,
            },
        },
    )
    assert grant.status_code == 201, grant.text
    row = await db_session.scalar(
        select(HomeProgramGrant).where(
            HomeProgramGrant.id == UUID(grant.json()["id"])
        )
    )
    await db_session.refresh(row)
    assert row.consent_event_id == UUID(consent["id"])
    stored = await db_session.get(PatientSharingConsentEvent, row.consent_event_id)
    assert stored.decision == "granted"
