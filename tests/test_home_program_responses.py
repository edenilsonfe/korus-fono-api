"""F16 fase 2 — fronteira pública do programa de casa (Tarefa 5.2).

Cobre o contrato do plano §3.5: header ``X-Home-Program-Token`` sem cookie/JWT,
GET público mínimo (sem metas, critérios, contatos ou IDs alheios), check-in
idempotente por ``clientRecordId`` (201 → 200 replay → 409 conflito), edição com
versão esperada e revisão anterior preservada, 410 genérico para todo estado
inválido (token, responsável removido, dono desativado, derivação ABA), 404 fora
do grant, 403 de entitlement do dono sem JWT, 422, 429 e 503 fail-closed.
SQLite em memória; a corrida real fica no gate PostgreSQL.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.auth_cookies import ACCESS_COOKIE
from app.core.security import create_access_token
from app.models.caregiver import Caregiver
from app.models.feature_flag import FeatureFlag
from app.models.goal import Goal
from app.models.home_program import (
    HomeProgramCheckIn,
    HomeProgramCheckInRevision,
    HomeProgramEvent,
    HomeProgramGrant,
    HomeProgramPhoto,
    HomeProgramTask,
)
from app.models.intervention_program import InterventionProgram
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense

TODAY = date.today()
TOKEN_HEADER = "X-Home-Program-Token"
PUBLIC_GET = "/api/v1/home-program-responses"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _headers(token: str | None) -> dict[str, str]:
    return {} if token is None else {TOKEN_HEADER: token}


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


async def _consent(api_client, auth_headers, patient: Patient, decision: str = "granted") -> dict:
    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sharing-consents",
        headers=auth_headers,
        json={"decision": decision, "policyVersion": "2026-09-09"},
    )
    assert response.status_code == 201, response.text
    return response.json()


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


async def _aba_program(
    db_session, patient: Patient, professional: Professional
) -> InterventionProgram:
    program = InterventionProgram(
        patient_id=patient.id,
        created_by_professional_id=professional.id,
        title="Ensino de tato",
        operational_definition="Nomeia figuras de animais.",
        teaching_strategy="Tentativas discretas.",
        mastery_percent=80,
        mastery_consecutive_sessions=3,
        status="active",
    )
    db_session.add(program)
    await db_session.commit()
    await db_session.refresh(program)
    return program


async def _resource(
    db_session, professional: Professional, *, family: bool = True
) -> tuple[Resource, ResourceLicense]:
    sha = "a" * 64
    resource = Resource(
        owner_professional_id=professional.id,
        title="Cartões de animais",
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
    task_count: int = 1,
    titles: list[str] | None = None,
    aba_id: UUID | None = None,
    resource_ids: list[UUID] | None = None,
) -> dict:
    goal = None
    if aba_id is None:
        goal = await _goal(db_session, patient, professional)
    task_titles = titles or [f"Atividade {index + 1}" for index in range(task_count)]
    body = {
        "title": "Rotina da casa",
        "startsOn": TODAY.isoformat(),
        "endsOn": (TODAY + timedelta(days=13)).isoformat(),
        "tasks": [
            _task(
                goal_id=goal.id if goal else None,
                aba_id=aba_id,
                resource_ids=resource_ids or [],
                title=title,
            )
            for title in task_titles
        ],
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


def _task_by_title(public: dict, title: str) -> dict:
    return next(task for task in public["tasks"] if task["title"] == title)


# --------------------------------------------------------------------------- #
# GET público mínimo
# --------------------------------------------------------------------------- #


async def test_public_get_projects_minimal_family_view(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    response = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {
        "id",
        "title",
        "patientFirstName",
        "professionalName",
        "startsOn",
        "endsOn",
        "expiresAt",
        "canRespond",
        "tasks",
    }
    assert data["id"] == program["id"]
    assert data["title"] == "Rotina da casa"
    assert data["patientFirstName"] == "João"  # só o primeiro nome
    assert data["professionalName"] == "Dra. Teste"
    assert data["canRespond"] is True
    assert _as_utc(datetime.fromisoformat(data["expiresAt"])) == _as_utc(
        grant.expires_at
    )

    task = data["tasks"][0]
    assert set(task) == {
        "id",
        "title",
        "instructions",
        "dueOn",
        "materials",
        "checkIn",
    }
    assert task["checkIn"] is None

    text = response.text
    for forbidden in (
        "Silva",  # nome completo do paciente/responsável
        professional.email,
        str(patient.id),
        "goalId",
        "interventionProgramId",
        "clientTaskId",
        token,
        "caregiverId",
    ):
        assert forbidden not in text


async def test_public_get_response_headers_are_private(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    response = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_family_token_does_not_open_professional_endpoints(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    response = await api_client.get(
        f"/api/v1/patients/{patient.id}/home-programs", headers=_headers(token)
    )
    assert response.status_code == 401


async def test_public_get_never_leaks_another_case(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    other_patient = Patient(
        professional_id=professional.id,
        name="Outra Criança",
        birth_date=date(2019, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(other_patient)
    await db_session.flush()
    db_session.add(
        Caregiver(
            patient_id=other_patient.id,
            name="Responsável Outra",
            relation="Pai",
            is_primary=True,
        )
    )
    await db_session.commit()
    await db_session.refresh(other_patient)

    other_program = await _published_program(
        api_client,
        auth_headers,
        other_patient,
        db_session,
        professional,
        titles=["Atividade do irmão"],
    )
    assert other_program["id"] != program["id"]

    response = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert response.status_code == 200, response.text
    text = response.text
    assert "Outra Criança" not in text
    assert "Atividade do irmão" not in text
    assert other_program["id"] not in text
    assert token not in text


# --------------------------------------------------------------------------- #
# 410 genérico — qualquer estado inválido do link
# --------------------------------------------------------------------------- #


async def test_missing_or_unknown_token_is_410_generic(api_client):
    details = []
    for headers in (
        {},
        {TOKEN_HEADER: ""},
        {TOKEN_HEADER: "   "},
        {TOKEN_HEADER: "unknown-token"},
        {TOKEN_HEADER: "x" * 129},
    ):
        response = await api_client.get(PUBLIC_GET, headers=headers)
        assert response.status_code == 410, (headers, response.text)
        details.append(response.json()["detail"])
    assert len(set(details)) == 1  # mesmo corpo genérico para todos os casos
    assert "indisponível" in details[0]
    assert "João" not in details[0]


async def test_410_after_rotation_revokes_previous_link(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    first_token, first_grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    second_token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(first_token))
    ).status_code == 410
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(second_token))
    ).status_code == 200
    await db_session.refresh(first_grant)
    assert first_grant.revoked_at is not None


async def test_410_after_revoke_and_expiry(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    revoked = await api_client.delete(
        f"/api/v1/patients/{patient.id}/home-programs/{program['id']}/grants/"
        f"{grant.id}",
        headers=auth_headers,
    )
    assert revoked.status_code == 204
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(token))
    ).status_code == 410

    # Novo link + expiração: 410 mesmo antes de revogar.
    fresh_program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    fresh_token, fresh_grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, fresh_program
    )
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(fresh_token))
    ).status_code == 200
    fresh_grant.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(fresh_token))
    ).status_code == 410


async def test_410_for_archived_program_removed_caregiver_and_disabled_owner(
    api_client, auth_headers, db_session, patient, professional
):
    # programa arquivado (arquivar revoga os grants na mesma transação)
    archived_program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    archived_token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, archived_program
    )
    archived = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs/"
        f"{archived_program['id']}/archive",
        headers=auth_headers,
    )
    assert archived.status_code == 200
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(archived_token))
    ).status_code == 410

    # responsável removido (FK SET NULL + revogação)
    removed_program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    removed_token, removed_grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, removed_program
    )
    removed_grant.caregiver_id = None
    removed_grant.revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(removed_token))
    ).status_code == 410

    # dono desativado
    disabled_program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    disabled_token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, disabled_program
    )
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(disabled_token))
    ).status_code == 200
    professional.is_disabled = True
    await db_session.commit()
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(disabled_token))
    ).status_code == 410


async def test_410_when_aba_derivation_is_invalidated(
    api_client, auth_headers, db_session, patient, professional
):
    await _enable_aba_flag(db_session)
    await _consent(api_client, auth_headers, patient)
    aba = await _aba_program(db_session, patient, professional)
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, aba_id=aba.id
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    assert (await api_client.get(PUBLIC_GET, headers=_headers(token))).status_code == 200

    # retirada do consentimento assistencial invalida a derivação ABA
    await _consent(api_client, auth_headers, patient, decision="withdrawn")
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(token))
    ).status_code == 410

    # concessão posterior do consentimento não ressuscita o link revogado;
    # um novo link só volta a valer com o gate vigente
    await _consent(api_client, auth_headers, patient, decision="granted")
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(token))
    ).status_code == 410  # grant original segue revogado pela retirada


async def test_410_when_aba_flag_is_disabled(
    api_client, auth_headers, db_session, patient, professional
):
    await _enable_aba_flag(db_session)
    await _consent(api_client, auth_headers, patient)
    aba = await _aba_program(db_session, patient, professional)
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, aba_id=aba.id
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    assert (await api_client.get(PUBLIC_GET, headers=_headers(token))).status_code == 200

    flag = await db_session.get(FeatureFlag, "multidisciplinary_aba")
    flag.enabled_global = False
    await db_session.commit()
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(token))
    ).status_code == 410


# --------------------------------------------------------------------------- #
# canRespond / entitlement sem JWT
# --------------------------------------------------------------------------- #


async def test_can_respond_false_in_read_only_without_financial_reason(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    professional.subscription_status = "canceled"
    await db_session.commit()

    response = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["canRespond"] is False  # leitura continua em read-only
    assert len(data["tasks"]) == 1
    lowered = response.text.lower()
    for word in ("assinatura", "plano", "pagamento", "billing", "trial"):
        assert word not in lowered


async def test_read_only_owner_blocks_check_in_writes_with_403(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    professional.subscription_status = "canceled"
    await db_session.commit()

    task_id = program["tasks"][0]["id"]
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert created.status_code == 403, created.text
    assert "indisponíve" in created.json()["detail"]
    assert "assinatura" not in created.json()["detail"].lower()

    updated = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{uuid4()}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": None,
        },
    )
    assert updated.status_code == 403

    count = await db_session.scalar(
        select(func.count()).select_from(HomeProgramCheckIn)
    )
    assert count == 0


async def test_stale_professional_cookie_neither_blocks_nor_bypasses_family_path(
    api_client, auth_headers, db_session, patient, professional
):
    """A família não tem JWT: um cookie profissional na mesma máquina não pode
    bloquear o caminho público (exceção de entitlement do prefixo), mas também
    não pode liberar escrita que a regra de produto nega (can_write do dono)."""
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    task_id = program["tasks"][0]["id"]
    stale_cookie = {ACCESS_COOKIE: create_access_token(professional.id)}

    # Dono com escrita normal: a presença do cookie não atrapalha o check-in.
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        cookies=stale_cookie,
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert created.status_code == 201, created.text

    # Dono vira read-only: o 403 tem de vir da REGRA DE PRODUTO (serviço),
    # não do middleware — senão carregaria type=entitlement_error.
    professional.subscription_status = "canceled"
    await db_session.commit()
    blocked = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{created.json()['id']}",
        headers=_headers(token),
        cookies=stale_cookie,
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": None,
        },
    )
    assert blocked.status_code == 403, blocked.text
    body = blocked.json()
    assert body.get("type") != "entitlement_error"
    assert "assinatura" not in str(body.get("detail", "")).lower()

    # E o dono read-only continua bloqueado nas próprias mutações (middleware).
    own = await api_client.post(
        f"/api/v1/patients/{patient.id}/home-programs",
        headers=auth_headers,
        json={"title": "Outro programa"},
    )
    assert own.status_code == 403
    assert own.json().get("type") == "entitlement_error"


# --------------------------------------------------------------------------- #
# Check-in: criação, replay e conflitos
# --------------------------------------------------------------------------- #


async def test_create_check_in_201_then_replay_200(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    task_id = program["tasks"][0]["id"]
    client_id = uuid4()
    payload = {
        "clientRecordId": str(client_id),
        "done": True,
        "comment": "  Fizemos a atividade.  ",
    }

    before = datetime.now(UTC) - timedelta(seconds=5)
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json=payload,
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["taskId"] == task_id
    assert data["done"] is True
    assert data["comment"] == "Fizemos a atividade."  # normalizado pelo servidor
    assert data["version"] == 1
    assert data["hasPhoto"] is False
    responded = _as_utc(datetime.fromisoformat(data["respondedAt"]))
    assert responded >= before  # timestamp do servidor, nunca do cliente

    again = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json=payload,
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == data["id"]
    assert again.json()["version"] == 1

    rows = (
        (
            await db_session.execute(
                select(HomeProgramCheckIn).where(
                    HomeProgramCheckIn.task_id == UUID(task_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].grant_id == grant.id
    events = await db_session.scalar(
        select(func.count())
        .select_from(HomeProgramEvent)
        .where(HomeProgramEvent.task_id == UUID(task_id))
    )
    assert events == 1

    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    check_in = _task_by_title(public, "Atividade 1")["checkIn"]
    assert check_in["id"] == data["id"]
    assert check_in["done"] is True
    assert check_in["comment"] == "Fizemos a atividade."


async def test_absence_and_negative_response_are_distinct(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, task_count=2
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    assert _task_by_title(public, "Atividade 1")["checkIn"] is None
    assert _task_by_title(public, "Atividade 2")["checkIn"] is None

    first_task = _task_by_title(public, "Atividade 1")
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{first_task['id']}/check-ins",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "done": False,
            "comment": "Não conseguimos fazer hoje.",
        },
    )
    assert created.status_code == 201, created.text

    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    done_false = _task_by_title(public, "Atividade 1")["checkIn"]
    assert done_false["done"] is False  # "informou que não fez"
    assert done_false["comment"] == "Não conseguimos fazer hoje."
    assert _task_by_title(public, "Atividade 2")["checkIn"] is None  # não informado


async def test_second_creation_and_record_reuse_are_conflicts(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, task_count=2
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    task_one = program["tasks"][0]["id"]
    task_two = program["tasks"][1]["id"]
    client_id = str(uuid4())
    payload = {"clientRecordId": client_id, "done": True, "comment": None}

    first = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_one}/check-ins",
        headers=_headers(token),
        json=payload,
    )
    assert first.status_code == 201

    # mesmo clientRecordId com payload diferente → 409
    reused = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_one}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": client_id, "done": False, "comment": None},
    )
    assert reused.status_code == 409
    assert "já foi usado" in reused.json()["detail"]

    # segunda criação na mesma tarefa (outro clientRecordId) → 409
    duplicate = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_one}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert duplicate.status_code == 409
    assert "já foi respondida" in duplicate.json()["detail"]

    # mesmo clientRecordId em outra tarefa também é payload diferente → 409
    other_task = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_two}/check-ins",
        headers=_headers(token),
        json=payload,
    )
    assert other_task.status_code == 409

    count = await db_session.scalar(
        select(func.count()).select_from(HomeProgramCheckIn)
    )
    assert count == 1


async def test_objects_outside_the_grant_are_404(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    other = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    other_token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, other
    )
    other_task_id = UUID(other["tasks"][0]["id"])

    # tarefa de outro programa/rolo com o token do primeiro → 404
    foreign = await api_client.post(
        f"{PUBLIC_GET}/tasks/{other_task_id}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert foreign.status_code == 404

    unknown = await api_client.post(
        f"{PUBLIC_GET}/tasks/{uuid4()}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert unknown.status_code == 404

    # resposta de outro grant → 404 no PATCH
    other_check = await api_client.post(
        f"{PUBLIC_GET}/tasks/{other_task_id}/check-ins",
        headers=_headers(other_token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert other_check.status_code == 201, other_check.text
    foreign_patch = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{other_check.json()['id']}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": None,
        },
    )
    assert foreign_patch.status_code == 404
    unknown_patch = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{uuid4()}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": None,
        },
    )
    assert unknown_patch.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"clientRecordId": "not-a-uuid", "done": True},
        {"clientRecordId": str(uuid4())},  # falta done
        {"clientRecordId": str(uuid4()), "done": True, "comment": "c" * 2001},
        {"clientRecordId": str(uuid4()), "done": True, "extra": "nope"},
        {
            "clientRecordId": str(uuid4()),
            "done": True,
            "comment": None,
            "respondedAt": "2020-01-01T00:00:00+00:00",
        },
    ],
)
async def test_create_rejects_invalid_payloads_with_422(
    api_client, auth_headers, db_session, patient, professional, payload
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    task_id = program["tasks"][0]["id"]

    response = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json=payload,
    )
    assert response.status_code == 422, response.text
    count = await db_session.scalar(
        select(func.count()).select_from(HomeProgramCheckIn)
    )
    assert count == 0


async def test_comment_boundary_is_2000_characters(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, task_count=2
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    accepted = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "done": True,
            "comment": "c" * 2000,
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert len(accepted.json()["comment"]) == 2000

    rejected = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][1]['id']}/check-ins",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "done": True,
            "comment": "c" * 2001,
        },
    )
    assert rejected.status_code == 422


# --------------------------------------------------------------------------- #
# PATCH: versão, revisão e replay
# --------------------------------------------------------------------------- #


async def test_rotation_does_not_transfer_previous_authoring(
    api_client, auth_headers, db_session, patient, professional
):
    """O responsável continua com o grant novo, mas a autoria antiga permanece."""
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    first_token, first_grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(first_token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": "Feito."},
    )
    assert created.status_code == 201, created.text
    check_in_id = created.json()["id"]

    second_token, second_grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    # o link antigo morreu; o novo permite continuar a tarefa
    assert (
        await api_client.get(PUBLIC_GET, headers=_headers(first_token))
    ).status_code == 410

    updated = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(second_token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": False,
            "comment": "Corrigindo.",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    check_in = await db_session.get(HomeProgramCheckIn, UUID(check_in_id))
    await db_session.refresh(check_in)
    assert check_in.grant_id == second_grant.id  # conteúdo atual é do grant novo
    revision = await db_session.scalar(
        select(HomeProgramCheckInRevision).where(
            HomeProgramCheckInRevision.check_in_id == UUID(check_in_id)
        )
    )
    assert revision.grant_id == first_grant.id  # autoria antiga preservada


async def test_patch_creates_revision_and_enforces_version(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    task_id = program["tasks"][0]["id"]
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{task_id}/check-ins",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "done": True,
            "comment": "Fizemos tudo.",
        },
    )
    assert created.status_code == 201
    check_in_id = created.json()["id"]

    update_payload = {
        "clientRecordId": str(uuid4()),
        "expectedVersion": 1,
        "done": False,
        "comment": "Na verdade não deu.",
    }
    updated = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(token),
        json=update_payload,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["done"] is False
    assert updated.json()["comment"] == "Na verdade não deu."

    revisions = (
        (
            await db_session.execute(
                select(HomeProgramCheckInRevision).where(
                    HomeProgramCheckInRevision.check_in_id == UUID(check_in_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision.version == 1
    assert revision.done is True
    assert revision.comment == "Fizemos tudo."
    assert revision.grant_id == grant.id  # ator familiar verdadeiro

    # replay idêntico → 200 com o mesmo resultado, sem nova revisão
    replay = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(token),
        json=update_payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["version"] == 2
    revision_count = await db_session.scalar(
        select(func.count())
        .select_from(HomeProgramCheckInRevision)
        .where(HomeProgramCheckInRevision.check_in_id == UUID(check_in_id))
    )
    assert revision_count == 1

    # mesmo clientRecordId com payload diferente → 409
    reused = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(token),
        json={**update_payload, "done": True},
    )
    assert reused.status_code == 409
    assert "já foi usado" in reused.json()["detail"]

    # versão desatualizada (novo comando) → 409
    stale = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 1,
            "done": True,
            "comment": None,
        },
    )
    assert stale.status_code == 409
    assert "desatualizada" in stale.json()["detail"]

    # novo comando com a versão certa → v3, revisão v2 preservada
    advanced = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{check_in_id}",
        headers=_headers(token),
        json={
            "clientRecordId": str(uuid4()),
            "expectedVersion": 2,
            "done": True,
            "comment": None,
        },
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["version"] == 3
    revision_versions = sorted(
        row.version
        for row in (
            (
                await db_session.execute(
                    select(HomeProgramCheckInRevision).where(
                        HomeProgramCheckInRevision.check_in_id
                        == UUID(check_in_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    )
    assert revision_versions == [1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        {"clientRecordId": str(uuid4()), "expectedVersion": 1, "done": True, "extra": 1},
        {"clientRecordId": str(uuid4()), "expectedVersion": 0, "done": True},
        {"clientRecordId": str(uuid4()), "done": True},  # falta expectedVersion
        {"clientRecordId": "nope", "expectedVersion": 1, "done": True},
    ],
)
async def test_patch_rejects_invalid_payloads_with_422(
    api_client, auth_headers, db_session, patient, professional, payload
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert created.status_code == 201

    response = await api_client.patch(
        f"{PUBLIC_GET}/check-ins/{created.json()['id']}",
        headers=_headers(token),
        json=payload,
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# Materiais, foto (hasPhoto) e rate limit
# --------------------------------------------------------------------------- #


async def test_materials_list_license_availability(
    api_client, auth_headers, db_session, patient, professional
):
    resource, license_row = await _resource(db_session, professional, family=True)
    program = await _published_program(
        api_client,
        auth_headers,
        patient,
        db_session,
        professional,
        resource_ids=[resource.id],
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    materials = public["tasks"][0]["materials"]
    assert materials == [
        {
            "id": str(resource.id),
            "title": "Cartões de animais",
            "attribution": "Uso autorizado pela autora",
            "available": True,
        }
    ]

    # retirada da licença bloqueia o material sem apagar instruções/resposta
    license_row.status = "revoked"
    await db_session.commit()
    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    materials = public["tasks"][0]["materials"]
    assert materials[0]["available"] is False
    assert public["tasks"][0]["instructions"]  # instruções continuam


async def test_has_photo_reflects_current_photo(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert created.status_code == 201
    check_in_id = UUID(created.json()["id"])

    db_session.add(
        HomeProgramPhoto(
            check_in_id=check_in_id,
            program_id=UUID(program["id"]),
            status="ready",
            content_type="image/jpeg",
            size_bytes=10,
            sha256="b" * 64,
            storage_key="patients/x/home-programs/y/photos/z.jpg",
        )
    )
    await db_session.commit()

    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    check_in = public["tasks"][0]["checkIn"]
    assert check_in["hasPhoto"] is True

    # foto deletada não conta mais
    photo = await db_session.scalar(
        select(HomeProgramPhoto).where(HomeProgramPhoto.check_in_id == check_in_id)
    )
    photo.status = "deleted"
    await db_session.commit()
    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    assert public["tasks"][0]["checkIn"]["hasPhoto"] is False


async def test_endpoint_limits_ip_then_hashed_grant(
    api_client, auth_headers, db_session, patient, professional, monkeypatch
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, grant = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
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

    response = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert response.status_code == 200
    assert calls[0]["key"].startswith("clinical:home-program-response:ip:")
    assert calls[0]["key"].endswith(_sha("127.0.0.1"))
    assert calls[0]["max_requests"] == 120 and calls[0]["window_seconds"] == 60
    assert calls[1]["key"] == (
        f"clinical:home-program-response:read:{_sha(str(grant.id))}"
    )
    assert calls[1]["max_requests"] == 60 and calls[1]["window_seconds"] == 60

    calls.clear()
    created = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert created.status_code == 201, created.text
    assert calls[0]["key"].startswith("clinical:home-program-response:ip:")
    assert calls[1]["key"] == (
        f"clinical:home-program-response:write:{_sha(str(grant.id))}"
    )
    assert calls[1]["max_requests"] == 30 and calls[1]["window_seconds"] == 60

    assert all(token not in call["key"] for call in calls)


async def test_rate_limited_public_calls_return_429_with_retry_after(
    api_client, auth_headers, db_session, patient, professional, monkeypatch
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: False
    )

    read = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert read.status_code == 429
    assert read.headers.get("Retry-After") == "60"

    write = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert write.status_code == 429
    assert write.headers.get("Retry-After") == "60"

    count = await db_session.scalar(
        select(func.count()).select_from(HomeProgramCheckIn)
    )
    assert count == 0


async def test_public_calls_fail_closed_without_counter_store(
    api_client, auth_headers, db_session, patient, professional, monkeypatch
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )

    def _store_down(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", _store_down
    )

    write = await api_client.post(
        f"{PUBLIC_GET}/tasks/{program['tasks'][0]['id']}/check-ins",
        headers=_headers(token),
        json={"clientRecordId": str(uuid4()), "done": True, "comment": None},
    )
    assert write.status_code == 503
    assert "indisponível" in write.json()["detail"]

    read = await api_client.get(PUBLIC_GET, headers=_headers(token))
    assert read.status_code == 503  # o limiter público é fail-closed por inteiro

    count = await db_session.scalar(
        select(func.count()).select_from(HomeProgramCheckIn)
    )
    assert count == 0


# --------------------------------------------------------------------------- #
# Scoping estrutural auxiliar
# --------------------------------------------------------------------------- #


async def test_public_tasks_are_limited_to_the_granted_program(
    api_client, auth_headers, db_session, patient, professional
):
    program = await _published_program(
        api_client, auth_headers, patient, db_session, professional, task_count=2
    )
    token, _ = await _issue_grant(
        api_client, auth_headers, db_session, patient, program
    )
    public = (await api_client.get(PUBLIC_GET, headers=_headers(token))).json()
    task_ids = {task["id"] for task in public["tasks"]}
    assert task_ids == {program["tasks"][0]["id"], program["tasks"][1]["id"]}
    stored = (
        (
            await db_session.execute(
                select(HomeProgramTask).where(
                    HomeProgramTask.program_id == UUID(program["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {str(row.id) for row in stored} == task_ids
