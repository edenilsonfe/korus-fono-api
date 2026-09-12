"""F14 onda 3 — materiais F17 no portal da família (Tarefa 3.1, §3.5).

Cobre: candidato do picker com avaliação explícita do gate familiar
(``assert_can_deliver_to_family``), publicação com metadados tipados
(licença/versão/hash/MIME/tamanho/attribution), entrega do ARQUIVO com
revalidação REAL da licença e do hash dos bytes a CADA leitura, teto de bytes
de 20 MiB, objeto ausente/divergente -> 409, storage fora -> 503, revalidação
PÓS-I/O (revogação/retirada durante o download descarta os bytes: 409/410/404)
e referência F14 que bloqueia substituir/excluir o ``Resource`` (confirmada no
teste de recursos; aqui via ``resource_has_references`` direto). SQLite em
memória; storage sempre stub local.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.caregiver import Caregiver
from app.models.family_portal_content import FamilyPortalItemRevision
from app.models.professional import Professional
from app.models.resource import Resource
from app.models.resource_license import ResourceLicense
from app.schemas.family_portal import FamilyPortalWithdrawRequest
from app.services import family_portal_access, family_portal_content
from app.services.resource_license_service import ARCHIVED_REASON
from app.services.resource_service import resource_has_references

TODAY = date.today()
TOKEN_HEADER = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"
PDF_BYTES = b"%PDF-1.4 conteudo-do-material-familiar"
MATERIAL_MAX_BYTES = 20 * 1024 * 1024
CONTENT_UNAVAILABLE = "Este conteúdo não está disponível no momento."


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


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
    """Storage em memória para publicação (verificação) e entrega (bytes)."""
    objects: dict[str, bytes] = {}
    content_types: dict[str, str] = {}
    calls: list[dict] = []

    async def fake_download_limited(
        key: str, max_bytes: int, timeout_seconds: float = 30.0
    ) -> tuple[bytes, str | None]:
        calls.append({"key": key, "max_bytes": max_bytes})
        if key not in objects:
            raise FakeMissingKeyError(key)
        return objects[key], content_types.get(key)

    async def forbidden_presign(*args, **kwargs):
        raise AssertionError("material familiar nunca usa presigned URL")

    for path in (
        "app.services.family_portal_files.storage_service.download_limited",
        "app.services.resource_license_service.storage_service.download_limited",
    ):
        monkeypatch.setattr(path, fake_download_limited)
    monkeypatch.setattr(
        "app.services.storage.storage_service.presigned_url", forbidden_presign
    )
    return SimpleNamespace(
        objects=objects, content_types=content_types, calls=calls
    )


def _seed_material(fake_material_storage, resource: Resource, body: bytes = PDF_BYTES) -> None:
    fake_material_storage.objects[resource.storage_key] = body
    fake_material_storage.content_types[resource.storage_key] = "application/pdf"


# --------------------------------------------------------------------------- #
# Helpers de portal (mesmos padrões dos focados da onda 2)
# --------------------------------------------------------------------------- #


def _base(patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _primary_caregiver(db_session, patient) -> Caregiver:
    caregiver = await db_session.scalar(
        select(Caregiver).where(
            Caregiver.patient_id == patient.id, Caregiver.is_primary.is_(True)
        )
    )
    assert caregiver is not None
    return caregiver


async def _caregiver(db_session, patient, *, name: str = "Responsável") -> Caregiver:
    caregiver = Caregiver(
        patient_id=patient.id, name=name, relation="Mãe", is_primary=False
    )
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


async def _enable(api_client, headers, patient) -> dict:
    response = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _authorize(
    api_client, headers, patient, caregiver_id, *, appointments: bool = False
) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json={
            "appointmentsEnabled": appointments,
            "familyAuthorization": {
                "authorizedAt": (
                    datetime.now(UTC) - timedelta(days=1)
                ).isoformat(),
                "reference": "Termo de autorização arquivado",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue(
    api_client, headers, patient, recipient_id
) -> tuple[str, dict]:
    response = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/grants",
        headers=headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": 1,
            "rotateFromGrantId": None,
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return data["url"].split("#token=", 1)[1], data


async def _ready_recipient(api_client, headers, patient, db_session) -> tuple[str, dict]:
    """Habilita o portal, autoriza o responsável principal e emite o link."""
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    token, _ = await _issue(api_client, headers, patient, recipient["id"])
    return token, recipient


async def _create_item(
    api_client,
    headers,
    patient,
    *,
    kind: str,
    source,
    content: dict,
    recipient_ids: list[str] | None = None,
):
    return await api_client.post(
        f"{_base(patient)}/items",
        headers=headers,
        json={
            "kind": kind,
            "source": source,
            "content": content,
            "recipientIds": recipient_ids or [],
        },
    )


async def _publish(
    api_client,
    headers,
    patient,
    item_id,
    *,
    expected_version: int,
    fingerprint: str | None,
):
    return await api_client.post(
        f"{_base(patient)}/items/{item_id}/publish",
        headers=headers,
        json={
            "expectedVersion": expected_version,
            "expectedSourceFingerprint": fingerprint,
            "reviewed": True,
        },
    )


# --------------------------------------------------------------------------- #
# Recursos sintéticos
# --------------------------------------------------------------------------- #


async def _resource(
    db_session,
    professional: Professional,
    *,
    family: bool = True,
    license_status: str = "declared",
    allow_professional: bool = True,
    title: str = "Cartões de animais",
    body: bytes = PDF_BYTES,
    valid_until: date | None = None,
    publication_status: str = "draft",
    is_global: bool = False,
) -> tuple[Resource, ResourceLicense]:
    digest = _sha256(body)
    resource = Resource(
        owner_professional_id=None if is_global else professional.id,
        title=title,
        description="Descrição",
        categories=["Linguagem"],
        format="PDF",
        file_size_bytes=len(body),
        author=professional.name,
        storage_key=f"resources/test/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
        content_sha256=digest,
        publication_status=publication_status,
    )
    db_session.add(resource)
    await db_session.flush()
    license_row = ResourceLicense(
        resource_id=resource.id,
        version=1,
        status=license_status,
        origin="original",
        rights_holder=professional.name,
        attribution="Uso autorizado pela autora",
        allow_professional_distribution=allow_professional,
        allow_family_delivery=family,
        content_sha256=digest,
        valid_until=valid_until,
    )
    db_session.add(license_row)
    await db_session.commit()
    await db_session.refresh(resource)
    await db_session.refresh(license_row)
    return resource, license_row


async def _publish_material(
    api_client,
    headers,
    patient,
    db_session,
    resource: Resource,
    *,
    instructions: str = "Mostre os cartões e aponte os animais.",
    recipient_id: str | None = None,
    title: str = "Cartões para casa",
) -> dict:
    """Cria rascunho + publica um material; devolve o JSON do item publicado."""
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(resource.id)},
        content={"title": title, "instructions": instructions},
        recipient_ids=[recipient_id] if recipient_id else [],
    )
    assert created.status_code == 201, created.text
    data = created.json()
    published = await _publish(
        api_client,
        headers,
        patient,
        data["id"],
        expected_version=data["version"],
        fingerprint=data["sourceFingerprint"],
    )
    assert published.status_code == 200, published.text
    return published.json()


async def _public_material_env(
    api_client,
    headers,
    patient,
    db_session,
    professional,
    fake_material_storage,
    *,
    family: bool = True,
    license_status: str = "declared",
    body: bytes = PDF_BYTES,
):
    """Ambiente completo: recurso + portal + destinatário + link + item publicado."""
    resource, license_row = await _resource(
        db_session,
        professional,
        family=family,
        license_status=license_status,
        body=body,
    )
    _seed_material(fake_material_storage, resource, body)
    token, recipient = await _ready_recipient(
        api_client, headers, patient, db_session
    )
    published = await _publish_material(
        api_client,
        headers,
        patient,
        db_session,
        resource,
        recipient_id=recipient["id"],
    )
    return SimpleNamespace(
        token=token,
        recipient=recipient,
        resource=resource,
        license=license_row,
        item=published,
    )


# --------------------------------------------------------------------------- #
# Candidatos do picker (elegibilidade explícita)
# --------------------------------------------------------------------------- #


async def test_material_candidates_evaluate_family_gate(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)

    eligible_resource, _ = await _resource(db_session, professional)
    no_license = Resource(
        owner_professional_id=professional.id,
        title="Sem licença",
        description="",
        categories=[],
        format="PDF",
        file_size_bytes=10,
        author=professional.name,
        storage_key=f"resources/test/{uuid.uuid4().hex}.pdf",
        content_type="application/pdf",
    )
    no_family, _ = await _resource(
        db_session, professional, family=False, title="Sem entrega familiar"
    )
    pending, _ = await _resource(
        db_session, professional, license_status="pending", title="Pendente"
    )
    expired, _ = await _resource(
        db_session,
        professional,
        valid_until=TODAY - timedelta(days=1),
        title="Expirado",
    )
    archived, _ = await _resource(
        db_session, professional, title="Arquivado", publication_status="archived"
    )
    db_session.add(no_license)
    await db_session.commit()

    # Global publicado com licença aprovada para profissionais.
    global_ok, _ = await _resource(
        db_session,
        professional,
        is_global=True,
        license_status="approved",
        publication_status="published",
        title="Global visível",
    )
    # Global publicado mas sem permissão familiar (visível e inelegível).
    global_no_family, _ = await _resource(
        db_session,
        professional,
        is_global=True,
        license_status="approved",
        family=False,
        publication_status="published",
        title="Global sem família",
    )
    # Global em rascunho não é visível.
    global_draft, _ = await _resource(
        db_session,
        professional,
        is_global=True,
        license_status="approved",
        title="Global rascunho",
    )
    # Material privado de outro profissional não é visível.
    other = Professional(
        email=f"outro-{uuid4().hex}@example.com", name="Outro", password_hash="x"
    )
    db_session.add(other)
    await db_session.commit()
    foreign, _ = await _resource(db_session, other, title="De outro profissional")

    response = await api_client.get(
        f"{_base(patient)}/sources?kind=resource", headers=headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    by_id = {item["id"]: item for item in payload["items"]}

    first = by_id[str(eligible_resource.id)]
    assert set(first.keys()) == {
        "id",
        "kind",
        "label",
        "date",
        "sourceFingerprint",
        "eligible",
        "unavailableReason",
    }
    assert first["kind"] == "resource"
    assert first["label"] == "Cartões de animais"
    assert first["eligible"] is True
    assert first["unavailableReason"] is None
    assert len(first["sourceFingerprint"]) == 64

    assert by_id[str(no_family.id)]["eligible"] is False
    assert "sem permissão de entrega à família" in by_id[str(no_family.id)][
        "unavailableReason"
    ]
    assert by_id[str(pending.id)]["eligible"] is False
    assert by_id[str(expired.id)]["eligible"] is False
    assert by_id[str(archived.id)]["eligible"] is False
    assert ARCHIVED_REASON in by_id[str(archived.id)]["unavailableReason"]
    assert by_id[str(global_ok.id)]["eligible"] is True
    assert by_id[str(global_no_family.id)]["eligible"] is False

    assert str(global_draft.id) not in by_id  # rascunho global não é visível
    assert str(foreign.id) not in by_id  # material alheio não é redistribuído

    # Material sem licença aparece como do dono e inelegível (com motivo).
    no_license_response = await api_client.get(
        f"{_base(patient)}/sources?kind=resource&q=Sem licença", headers=headers
    )
    assert no_license_response.status_code == 200
    listed = no_license_response.json()["items"]
    assert len(listed) == 1 and listed[0]["id"] == str(no_license.id)
    assert listed[0]["eligible"] is False

    empty = await api_client.get(
        f"{_base(patient)}/sources?kind=resource&q=nao-existe", headers=headers
    )
    assert empty.json()["items"] == []


async def test_material_create_rejects_ineligible_source(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    pending, _ = await _resource(db_session, professional, license_status="pending")
    blocked = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(pending.id)},
        content={"title": "x"},
    )
    assert blocked.status_code == 422, blocked.text
    assert "curadoria" in blocked.json()["detail"]

    # Material privado de outro profissional → 404, sem criar rascunho.
    other = Professional(
        email=f"outro-{uuid4().hex}@example.com", name="Outro", password_hash="x"
    )
    db_session.add(other)
    await db_session.commit()
    foreign, _ = await _resource(db_session, other)
    alien = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(foreign.id)},
        content={"title": "x"},
    )
    assert alien.status_code == 404


# --------------------------------------------------------------------------- #
# Publicação com metadados congelados + entrega do arquivo
# --------------------------------------------------------------------------- #


async def test_material_publish_freezes_metadata_and_delivers_file(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    item = env.item

    # Revisão congelou licença/versão/hash/MIME/tamanho/attribution.
    revisions = await api_client.get(
        f"{_base(patient)}/items/{item['id']}/revisions", headers=headers
    )
    assert revisions.status_code == 200, revisions.text
    revision = revisions.json()["items"][0]
    metadata = revision["sourceMetadata"]
    assert metadata["resourceId"] == str(env.resource.id)
    assert metadata["licenseId"] == str(env.license.id)
    assert metadata["licenseVersion"] == 1
    assert metadata["sha256"] == _sha256(PDF_BYTES)
    assert metadata["contentType"] == "application/pdf"
    assert metadata["sizeBytes"] == len(PDF_BYTES)
    assert metadata["attribution"] == "Uso autorizado pela autora"

    # A publicação verificou o arquivo real com teto de 20 MiB.
    assert fake_material_storage.calls
    assert fake_material_storage.calls[0]["max_bytes"] == MATERIAL_MAX_BYTES

    # Listagem pública marca disponibilidade; detalhe traz a allowlist.
    listed = await api_client.get(
        f"{PUBLIC}/items?kind=material", headers=_headers(env.token)
    )
    assert listed.status_code == 200, listed.text
    listed_item = listed.json()["items"][0]
    assert listed_item["id"] == item["id"]
    assert listed_item["available"] is True

    detail = await api_client.get(
        f"{PUBLIC}/items/{item['id']}", headers=_headers(env.token)
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body.keys()) == {
        "id",
        "kind",
        "title",
        "publishedAt",
        "available",
        "instructions",
        "attribution",
        "contentType",
        "sizeBytes",
    }
    assert body["instructions"] == "Mostre os cartões e aponte os animais."
    assert body["attribution"] == "Uso autorizado pela autora"
    assert body["contentType"] == "application/pdf"
    assert body["sizeBytes"] == len(PDF_BYTES)

    # Arquivo: bytes privados pela API, nome seguro, sem presigned.
    file_response = await api_client.get(
        f"{PUBLIC}/items/{item['id']}/file", headers=_headers(env.token)
    )
    assert file_response.status_code == 200, file_response.text
    assert file_response.content == PDF_BYTES
    assert file_response.headers["content-type"] == "application/pdf"
    assert (
        f'material-{item["id"]}.pdf'
        in file_response.headers["content-disposition"]
    )
    assert file_response.headers["cache-control"] == "private, no-store"
    assert file_response.headers["x-content-type-options"] == "nosniff"
    assert file_response.headers["referrer-policy"] == "no-referrer"


async def test_material_public_availability_follows_license_state(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    url = f"{PUBLIC}/items/{env.item['id']}"
    file_url = f"{url}/file"

    # Revogação da licença bloqueia o ARQUIVO e marca indisponível (409/False),
    # sem expor o motivo real.
    env.license.status = "revoked"
    await db_session.commit()
    blocked = await api_client.get(file_url, headers=_headers(env.token))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == CONTENT_UNAVAILABLE
    detail = await api_client.get(url, headers=_headers(env.token))
    assert detail.json()["available"] is False
    assert detail.json()["unavailableReason"] == CONTENT_UNAVAILABLE
    listed = await api_client.get(
        f"{PUBLIC}/items?kind=material", headers=_headers(env.token)
    )
    assert listed.json()["items"][0]["available"] is False

    # Nova declaração familiar (mesmo arquivo) reabre a entrega.
    db_session.add(
        ResourceLicense(
            resource_id=env.resource.id,
            version=2,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            attribution="Uso autorizado pela autora",
            allow_family_delivery=True,
            content_sha256=env.resource.content_sha256,
        )
    )
    await db_session.commit()
    reopened = await api_client.get(file_url, headers=_headers(env.token))
    assert reopened.status_code == 200, reopened.text

    # Licença expirada também bloqueia.
    db_session.add(
        ResourceLicense(
            resource_id=env.resource.id,
            version=3,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            allow_family_delivery=True,
            content_sha256=env.resource.content_sha256,
            valid_until=TODAY - timedelta(days=1),
        )
    )
    await db_session.commit()
    expired = await api_client.get(file_url, headers=_headers(env.token))
    assert expired.status_code == 409

    # Arquivar o material bloqueia download sem apagar o histórico.
    archived = await api_client.post(
        f"/api/v1/resources/{env.resource.id}/archive", headers=headers, json={}
    )
    assert archived.status_code == 200, archived.text
    archived_file = await api_client.get(file_url, headers=_headers(env.token))
    assert archived_file.status_code == 409
    await db_session.refresh(env.resource)
    assert env.resource.storage_key is not None  # arquivo preservado


async def test_material_file_blocks_hash_divergence_and_missing_object(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    file_url = f"{PUBLIC}/items/{env.item['id']}/file"

    # Bytes divergentes do hash congelado na revisão → 409 (nunca versão antiga).
    fake_material_storage.objects[env.resource.storage_key] = b"%PDF-1.4 outro"
    mismatch = await api_client.get(file_url, headers=_headers(env.token))
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["detail"] == CONTENT_UNAVAILABLE

    # Banco diz um hash, arquivo atual é outro (troca por baixo) → 409.
    fake_material_storage.objects[env.resource.storage_key] = PDF_BYTES
    env.resource.content_sha256 = "b" * 64
    await db_session.commit()
    db_mismatch = await api_client.get(file_url, headers=_headers(env.token))
    assert db_mismatch.status_code == 409

    # Objeto desaparecido → 409 neutro (disponibilidade mudou), não 404.
    env.resource.content_sha256 = _sha256(PDF_BYTES)
    await db_session.commit()
    fake_material_storage.objects.pop(env.resource.storage_key)
    missing = await api_client.get(file_url, headers=_headers(env.token))
    assert missing.status_code == 409
    assert missing.json()["detail"] == CONTENT_UNAVAILABLE

    # Falha operacional de storage → 503 (sem servir cache).
    async def broken_download(*args, **kwargs):
        raise RuntimeError("storage fora do ar")

    import app.services.family_portal_files as files_module

    monkeypatch_target = files_module.storage_service
    original = monkeypatch_target.download_limited
    monkeypatch_target.download_limited = broken_download
    try:
        unavailable = await api_client.get(file_url, headers=_headers(env.token))
    finally:
        monkeypatch_target.download_limited = original
    assert unavailable.status_code == 503
    assert "indisponível" in unavailable.json()["detail"]


async def test_material_publish_storage_failure_is_503_or_conflict(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    resource, _ = await _resource(db_session, professional)
    _seed_material(fake_material_storage, resource)
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(resource.id)},
        content={"title": "x", "instructions": ""},
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    fingerprint = created.json()["sourceFingerprint"]

    # Storage fora → 503, rascunho permanece e nenhuma revisão é criada.
    async def broken_download(*args, **kwargs):
        raise RuntimeError("storage fora do ar")

    import app.services.resource_license_service as license_module

    original = license_module.storage_service.download_limited
    license_module.storage_service.download_limited = broken_download
    try:
        failed = await _publish(
            api_client,
            headers,
            patient,
            item_id,
            expected_version=1,
            fingerprint=fingerprint,
        )
    finally:
        license_module.storage_service.download_limited = original
    assert failed.status_code == 503, failed.text

    current = await api_client.get(f"{_base(patient)}/items/{item_id}", headers=headers)
    assert current.json()["status"] == "draft"
    assert current.json()["publishedVersion"] is None
    revisions = await db_session.scalar(
        select(FamilyPortalItemRevision).where(
            FamilyPortalItemRevision.item_id == UUID(item_id)
        )
    )
    assert revisions is None

    # Objeto ausente na verificação → 409 (hash não conferido nunca publica).
    fake_material_storage.objects.pop(resource.storage_key)
    missing = await _publish(
        api_client,
        headers,
        patient,
        item_id,
        expected_version=1,
        fingerprint=fingerprint,
    )
    assert missing.status_code == 409, missing.text


async def test_material_publish_requires_fingerprint_and_detects_source_change(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _primary_caregiver(db_session, patient)
    recipient = await _authorize(api_client, headers, patient, caregiver.id)
    resource, _ = await _resource(db_session, professional)
    _seed_material(fake_material_storage, resource)
    created = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(resource.id)},
        content={"title": "x", "instructions": ""},
        recipient_ids=[recipient["id"]],
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    # Sem fingerprint para fonte com fonte → 422.
    missing_fingerprint = await _publish(
        api_client, headers, patient, item_id, expected_version=1, fingerprint=None
    )
    assert missing_fingerprint.status_code == 422

    # Nova declaração de licença muda o fingerprint; a antiga vira 409.
    old_fingerprint = created.json()["sourceFingerprint"]
    db_session.add(
        ResourceLicense(
            resource_id=resource.id,
            version=2,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            allow_family_delivery=True,
            content_sha256=resource.content_sha256,
        )
    )
    await db_session.commit()
    stale = await _publish(
        api_client,
        headers,
        patient,
        item_id,
        expected_version=1,
        fingerprint=old_fingerprint,
    )
    assert stale.status_code == 409, stale.text
    assert "mudou" in stale.json()["detail"]

    # Com o fingerprint atual, a publicação passa e o rascunho sincroniza.
    current = await api_client.get(f"{_base(patient)}/items/{item_id}", headers=headers)
    assert current.json()["sourceChanged"] is True
    fresh = await _publish(
        api_client,
        headers,
        patient,
        item_id,
        expected_version=1,
        fingerprint=current.json()["sourceFingerprint"],
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["status"] == "published"
    assert fresh.json()["sourceChanged"] is False


# --------------------------------------------------------------------------- #
# Escopo do link e revalidação pós-I/O
# --------------------------------------------------------------------------- #


async def test_material_file_requires_grant_scope_and_kind(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    file_url = f"{PUBLIC}/items/{env.item['id']}/file"

    # Sem token / token desconhecido → MESMO 410 genérico.
    for token in (None, "invalido-token"):
        denied = await api_client.get(file_url, headers=_headers(token))
        assert denied.status_code == 410, token
        assert "inválido" in denied.json()["detail"]

    # Item fora do público do OUTRO destinatário → 404 (mesmo sendo do dono).
    other_caregiver = await _caregiver(db_session, patient, name="Outro responsável")
    other_recipient = await _authorize(
        api_client, headers, patient, other_caregiver.id
    )
    other_token, _ = await _issue(
        api_client, headers, patient, other_recipient["id"]
    )
    cross = await api_client.get(file_url, headers=_headers(other_token))
    assert cross.status_code == 404

    # Kind sem arquivo → 404 (sessão publicada é conteúdo, não download).
    from app.models.session import Session as SessionRow

    session_row = SessionRow(
        patient_id=patient.id,
        professional_id=professional.id,
        date=datetime.now(UTC) - timedelta(days=1),
        duration=50,
        type="Terapia",
        objectives=[],
        notes="notas",
    )
    db_session.add(session_row)
    await db_session.commit()
    await db_session.refresh(session_row)
    summary = await _create_item(
        api_client,
        headers,
        patient,
        kind="session_summary",
        source={"sessionId": str(session_row.id)},
        content={"title": "Sessão", "body": "Texto"},
        recipient_ids=[env.recipient["id"]],
    )
    published_summary = await _publish(
        api_client,
        headers,
        patient,
        summary.json()["id"],
        expected_version=1,
        fingerprint=summary.json()["sourceFingerprint"],
    )
    assert published_summary.status_code == 200, published_summary.text
    no_file = await api_client.get(
        f"{PUBLIC}/items/{summary.json()['id']}/file",
        headers=_headers(env.token),
    )
    assert no_file.status_code == 404

    # Rascunho de material não tem arquivo público (não está publicado).
    draft = await _create_item(
        api_client,
        headers,
        patient,
        kind="material",
        source={"resourceId": str(env.resource.id)},
        content={"title": "Rascunho", "instructions": ""},
        recipient_ids=[env.recipient["id"]],
    )
    assert draft.status_code == 201
    draft_file = await api_client.get(
        f"{PUBLIC}/items/{draft.json()['id']}/file", headers=_headers(env.token)
    )
    assert draft_file.status_code == 404


async def test_material_revalidates_after_io_revocation_and_withdrawal(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    file_url = f"{PUBLIC}/items/{env.item['id']}/file"

    # Revogação DURANTE o download: bytes são descartados → 409.
    async def revoke_during_download(key, max_bytes, timeout_seconds=30.0):
        env.license.status = "revoked"
        await db_session.commit()
        return PDF_BYTES, "application/pdf"

    fake_material_storage.objects[env.resource.storage_key] = PDF_BYTES
    import app.services.family_portal_files as files_module

    monkeypatch_target = files_module.storage_service
    original = monkeypatch_target.download_limited
    monkeypatch_target.download_limited = revoke_during_download
    try:
        revoked = await api_client.get(file_url, headers=_headers(env.token))
    finally:
        monkeypatch_target.download_limited = original
    assert revoked.status_code == 409, revoked.text
    assert revoked.json()["detail"] == CONTENT_UNAVAILABLE

    # Retirada do destinatário durante o download → 410 (grant cai).
    db_session.add(
        ResourceLicense(
            resource_id=env.resource.id,
            version=2,
            status="declared",
            origin="original",
            rights_holder=professional.name,
            allow_family_delivery=True,
            content_sha256=env.resource.content_sha256,
        )
    )
    await db_session.commit()

    async def withdraw_during_download(key, max_bytes, timeout_seconds=30.0):
        await family_portal_access.withdraw_recipient(
            db_session,
            patient.id,
            UUID(env.recipient["id"]),
            professional,
            FamilyPortalWithdrawRequest(reason="professional_decision"),
        )
        await db_session.commit()
        return PDF_BYTES, "application/pdf"

    monkeypatch_target.download_limited = withdraw_during_download
    try:
        gone = await api_client.get(file_url, headers=_headers(env.token))
    finally:
        monkeypatch_target.download_limited = original
    assert gone.status_code == 410, gone.text


async def test_material_withdraw_during_download_blocks_file(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    file_url = f"{PUBLIC}/items/{env.item['id']}/file"

    async def withdraw_during_download(key, max_bytes, timeout_seconds=30.0):
        await family_portal_content.withdraw_item(
            db_session,
            patient.id,
            professional,
            UUID(env.item["id"]),
        )
        await db_session.commit()
        return PDF_BYTES, "application/pdf"

    import app.services.family_portal_files as files_module

    target = files_module.storage_service
    original = target.download_limited
    target.download_limited = withdraw_during_download
    try:
        withdrawn = await api_client.get(file_url, headers=_headers(env.token))
    finally:
        target.download_limited = original
    assert withdrawn.status_code == 404, withdrawn.text


async def test_material_file_consumes_file_rate_limit(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage, monkeypatch
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    calls: list[dict] = []

    def fake_allow(*, key, max_requests, window_seconds):
        calls.append(
            {
                "key": key,
                "max_requests": max_requests,
                "window_seconds": window_seconds,
            }
        )
        return True

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", fake_allow
    )
    response = await api_client.get(
        f"{PUBLIC}/items/{env.item['id']}/file", headers=_headers(env.token)
    )
    assert response.status_code == 200, response.text
    assert calls[0]["key"].startswith("clinical:family-portal:ip:")
    assert calls[0]["max_requests"] == 120
    assert calls[1]["key"].startswith("clinical:family-portal:read:")
    assert calls[1]["max_requests"] == 60
    assert calls[2]["key"].startswith("clinical:family-portal:file:")
    assert calls[2]["max_requests"] == 10
    assert all(env.token not in call["key"] for call in calls)


# --------------------------------------------------------------------------- #
# Referência do item F14 bloqueia substituir/excluir o Resource
# --------------------------------------------------------------------------- #


async def test_material_reference_blocks_replace_and_delete_but_allows_archive(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )

    assert await resource_has_references(db_session, env.resource) is True

    blocked_delete = await api_client.delete(
        f"/api/v1/resources/{env.resource.id}", headers=headers
    )
    assert blocked_delete.status_code == 409, blocked_delete.text
    blocked_replace = await api_client.patch(
        f"/api/v1/resources/{env.resource.id}",
        headers=headers,
        data={"title": "Novo título"},
        files={"file": ("novo.pdf", b"%PDF-1.4 novo", "application/pdf")},
    )
    assert blocked_replace.status_code == 409, blocked_replace.text

    # Retirada do item (com histórico) continua bloqueando o que foi congelado.
    withdrawn = await api_client.post(
        f"{_base(patient)}/items/{env.item['id']}/withdraw", headers=headers, json={}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert await resource_has_references(db_session, env.resource) is True
    still_blocked = await api_client.delete(
        f"/api/v1/resources/{env.resource.id}", headers=headers
    )
    assert still_blocked.status_code == 409

    # Arquivar continua permitido e preserva o histórico.
    archived = await api_client.post(
        f"/api/v1/resources/{env.resource.id}/archive", headers=headers, json={}
    )
    assert archived.status_code == 200, archived.text


async def test_material_cross_professional_editorial_scope(
    api_client, auth_headers, db_session, patient, professional, fake_material_storage
):
    """Colega não lista fontes, não cria nem enxerga item do dono."""
    headers = auth_headers
    env = await _public_material_env(
        api_client, headers, patient, db_session, professional, fake_material_storage
    )
    other = Professional(
        email=f"colega-{uuid4().hex}@example.com",
        name="Colega",
        password_hash="x",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    other_headers = {
        "Authorization": f"Bearer {create_access_token(other.id)}"
    }

    sources = await api_client.get(
        f"{_base(patient)}/sources?kind=resource", headers=other_headers
    )
    assert sources.status_code == 404
    created = await _create_item(
        api_client,
        other_headers,
        patient,
        kind="material",
        source={"resourceId": str(env.resource.id)},
        content={"title": "x"},
    )
    assert created.status_code == 404
    detail = await api_client.get(
        f"{_base(patient)}/items/{env.item['id']}", headers=other_headers
    )
    assert detail.status_code == 404
