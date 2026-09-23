"""Business rules for pre-attendance requests, grants and uploads."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.utils import utcnow
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.intake import (
    INTAKE_CANCELLED,
    INTAKE_DRAFT,
    INTAKE_FORM_VERSION,
    INTAKE_REVIEWED,
    INTAKE_SUBMITTED,
    IntakeAuditEvent,
    IntakeFile,
    IntakeGrant,
    IntakeRequest,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.intake import (
    INTAKE_FORM_KEYS,
    IntakeAnswer,
    IntakeCreate,
    IntakeDraftPatch,
    IntakeGrantIssue,
    IntakeGrantResponse,
    IntakePublicResponse,
    IntakeRequestResponse,
    IntakeReview,
)
from app.schemas.prontuario import AnamneseEntryInput, AnamneseEntryResponse
from app.services import anamnese_service
from app.services.attachment_upload import (
    assert_declared_matches_sniff,
    normalize_content_type,
    sanitize_filename,
)
from app.services.entitlement_service import EntitlementService
from app.services.storage import safe_content_disposition_filename, storage_service
from app.services.storage_cleanup_service import queue_storage_cleanup, reserve_storage_cleanup, resolve_storage_cleanup
from app.utils.token_hash import hash_token

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 5
PUBLIC_GONE_MESSAGE = "Este link está inválido ou indisponível. Peça um novo link à profissional."
ALLOWED_FILE_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
INTAKE_SECTIONS = {
    "reasonForReferral": "Pré-atendimento — motivo da procura",
    "developmentHistory": "Pré-atendimento — histórico do desenvolvimento",
    "schooling": "Pré-atendimento — escolarização",
    "currentCare": "Pré-atendimento — acompanhamentos atuais",
    "routine": "Pré-atendimento — rotina",
    "expectations": "Pré-atendimento — expectativas",
    "additionalNotes": "Pré-atendimento — observações adicionais",
}


def _gone() -> HTTPException:
    return HTTPException(status_code=status.HTTP_410_GONE, detail=PUBLIC_GONE_MESSAGE, headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"})


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _conflict(detail: str = "O formulário foi alterado. Atualize e tente novamente.") -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _workflow_enabled(db: AsyncSession, owner_id: UUID) -> None:
    from app.services.clinical_workflow_flags import require_workflow_enabled
    await require_workflow_enabled(db, owner_id, "patient_intake")
    owner = await db.get(Professional, owner_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    await EntitlementService(db).ensure_write_allowed(owner)


def _validate_responses(responses: dict[str, IntakeAnswer | dict[str, Any]]) -> dict:
    if len(responses) > len(INTAKE_FORM_KEYS) or any(key not in INTAKE_FORM_KEYS for key in responses):
        raise HTTPException(status_code=422, detail="Campo de pré-atendimento inválido.")
    normalized: dict[str, dict[str, Any]] = {}
    for key, answer in responses.items():
        if isinstance(answer, IntakeAnswer):
            item = answer.model_dump(by_alias=True)
        elif isinstance(answer, dict):
            item = IntakeAnswer.model_validate(answer).model_dump(by_alias=True)
        else:
            raise HTTPException(status_code=422, detail="Resposta de pré-atendimento inválida.")
        if item.get("notKnown") and item.get("value"):
            raise HTTPException(status_code=422, detail="Escolha uma resposta ou 'Não sei informar'.")
        normalized[key] = item
    return normalized


def _payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _fingerprint(entries: list) -> str:
    payload = "\n".join(f"{entry.section}\0{entry.value}" for entry in sorted(entries, key=lambda e: e.section))
    return hashlib.sha256(payload.encode()).hexdigest()


async def anamnese_fingerprint(db: AsyncSession, patient_id: UUID) -> str:
    return _fingerprint(await anamnese_service.list_entries(db, patient_id))


async def require_owned_request(db: AsyncSession, patient_id: UUID, request_id: UUID, actor: Professional, *, lock: bool = False) -> IntakeRequest:
    query = select(IntakeRequest).where(IntakeRequest.id == request_id, IntakeRequest.patient_id == patient_id, IntakeRequest.owner_professional_id == actor.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    request = await db.scalar(query)
    if request is None:
        raise HTTPException(status_code=404, detail="Pré-atendimento não encontrado")
    return request


async def require_caregiver(db: AsyncSession, patient_id: UUID, caregiver_id: UUID) -> Caregiver:
    caregiver = await db.scalar(select(Caregiver).where(Caregiver.id == caregiver_id, Caregiver.patient_id == patient_id))
    if caregiver is None:
        raise HTTPException(status_code=422, detail="Responsável identificado não encontrado.")
    return caregiver


def _file_response(item: IntakeFile):
    from app.schemas.intake import IntakeFileResponse
    return IntakeFileResponse(id=str(item.id), name=item.name, content_type=item.content_type, size_bytes=item.size_bytes, created_at=item.created_at, incorporated=item.incorporated_attachment_id is not None)


async def request_response(db: AsyncSession, request: IntakeRequest) -> IntakeRequestResponse:
    caregiver = await db.get(Caregiver, request.caregiver_id) if request.caregiver_id else None
    files = (await db.execute(select(IntakeFile).where(IntakeFile.intake_request_id == request.id, IntakeFile.deleted_at.is_(None)).order_by(IntakeFile.created_at))).scalars().all()
    grant = await db.scalar(select(IntakeGrant).where(IntakeGrant.intake_request_id == request.id, IntakeGrant.revoked_at.is_(None)).order_by(IntakeGrant.created_at.desc()))
    patient = await db.get(Patient, request.patient_id)
    entries = await anamnese_service.list_entries(db, request.patient_id)
    current_fingerprint = _fingerprint(entries)
    grant_response = None
    if grant:
        grant_response = IntakeGrantResponse(id=str(grant.id), expires_at=grant.expires_at, revoked_at=grant.revoked_at, active=_as_utc(grant.expires_at) > utcnow())
    return IntakeRequestResponse(
        id=str(request.id), patient_id=str(request.patient_id), caregiver_id=str(request.caregiver_id) if request.caregiver_id else None, caregiver_name=caregiver.name if caregiver else request.caregiver_name_snapshot,
        form_version=request.form_version, status=request.status, responses=request.responses, version=request.version,
        created_at=request.created_at, updated_at=request.updated_at, submitted_at=request.submitted_at, reviewed_at=request.reviewed_at,
        anamnese_fingerprint=current_fingerprint, anamnese_status=patient.anamnese_status if patient else "draft",
        current_anamnese=[AnamneseEntryResponse(id=str(e.id), patient_id=str(e.patient_id), section=e.section, value=e.value) for e in entries], files=[_file_response(f) for f in files], current_grant=grant_response,
        selected_fields=list(request.selected_fields or []), selected_file_ids=[str(item) for item in (request.selected_file_ids or [])],
    )


async def create_request(db: AsyncSession, *, patient: Patient, actor: Professional, body: IntakeCreate) -> IntakeRequest:
    await _workflow_enabled(db, actor.id)
    caregiver = await require_caregiver(db, patient.id, body.caregiver_id)
    request = IntakeRequest(patient_id=patient.id, caregiver_id=caregiver.id, caregiver_name_snapshot=caregiver.name, owner_professional_id=actor.id, form_version=INTAKE_FORM_VERSION, responses={}, version=1)
    db.add(request)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict("Já existe um pré-atendimento aberto para este responsável.") from exc
    db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=patient.id, actor_professional_id=actor.id, event_type="created", payload={}, occurred_at=utcnow()))
    return request


async def list_requests(db: AsyncSession, patient_id: UUID, actor: Professional) -> list[IntakeRequest]:
    return list((await db.execute(select(IntakeRequest).where(IntakeRequest.patient_id == patient_id, IntakeRequest.owner_professional_id == actor.id).order_by(IntakeRequest.created_at.desc()))).scalars().all())


async def save_draft(db: AsyncSession, *, raw_token: str, request: IntakeRequest, body: IntakeDraftPatch) -> IntakeRequest:
    await _workflow_enabled(db, request.owner_professional_id)
    request = await db.scalar(select(IntakeRequest).where(IntakeRequest.id == request.id).with_for_update().execution_options(populate_existing=True))
    if request is None:
        raise HTTPException(status_code=404, detail="Pré-atendimento não encontrado")
    if request.status != INTAKE_DRAFT:
        raise _conflict("Respostas enviadas não podem ser editadas.")
    if request.version != body.expected_version:
        raise _conflict()
    request.responses = _validate_responses(body.responses)
    request.version += 1
    db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=request.patient_id, actor_caregiver_id=request.caregiver_id, event_type="draft_saved", payload={"version": request.version}, occurred_at=utcnow()))
    await db.flush()
    return request


async def submit(db: AsyncSession, *, request: IntakeRequest, body, raw_token: str) -> IntakeRequest:
    await _workflow_enabled(db, request.owner_professional_id)
    request = await db.scalar(select(IntakeRequest).where(IntakeRequest.id == request.id).with_for_update().execution_options(populate_existing=True))
    if request is None:
        raise HTTPException(status_code=404, detail="Pré-atendimento não encontrado")
    candidate = _validate_responses(body.responses) if body.responses is not None else request.responses
    candidate_hash = _payload_hash(candidate)
    if request.status in (INTAKE_SUBMITTED, INTAKE_REVIEWED) and request.submitted_command_key == body.command_key:
        if request.submitted_payload_hash != candidate_hash:
            raise _conflict("A mesma chave de envio foi usada com outro conteúdo.")
        return request
    if request.status != INTAKE_DRAFT:
        raise _conflict("Este pré-atendimento já foi enviado.")
    if request.version != body.expected_version:
        raise _conflict()
    if body.responses is not None:
        request.responses = candidate
    request.status = INTAKE_SUBMITTED
    request.submitted_at = utcnow()
    request.submitted_command_key = body.command_key
    request.submitted_payload_hash = candidate_hash
    request.version += 1
    db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=request.patient_id, actor_caregiver_id=request.caregiver_id, event_type="submitted", payload={"version": request.version}, occurred_at=utcnow()))
    await db.flush()
    return request


async def issue_grant(db: AsyncSession, *, request: IntakeRequest, actor: Professional, body: IntakeGrantIssue) -> tuple[IntakeGrant, str]:
    await _workflow_enabled(db, actor.id)
    request = await require_owned_request(db, request.patient_id, request.id, actor, lock=True)
    if request.caregiver_id is None:
        raise _conflict("O responsável deste pré-atendimento não está mais identificado.")
    old = await db.scalar(select(IntakeGrant).where(IntakeGrant.intake_request_id == request.id, IntakeGrant.revoked_at.is_(None)).with_for_update())
    if body.rotate_from_grant_id is not None and (old is None or old.id != body.rotate_from_grant_id):
        raise _conflict("O convite anterior não é o convite ativo.")
    now = utcnow()
    if old:
        old.revoked_at = now
    raw = secrets.token_urlsafe(32)
    grant = IntakeGrant(intake_request_id=request.id, caregiver_id=request.caregiver_id, token_hash=hash_token(raw), expires_at=now + timedelta(days=body.expires_in_days), authorization={"authorizedAt": body.authorized_at.isoformat(), "reference": body.reference, "reviewed": True}, created_by_professional_id=actor.id)
    db.add(grant)
    db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=request.patient_id, actor_professional_id=actor.id, event_type="grant_issued", payload={}, occurred_at=now))
    await db.flush()
    return grant, raw


async def revoke_grant(db: AsyncSession, *, request: IntakeRequest, grant_id: UUID, actor: Professional) -> None:
    grant = await db.scalar(select(IntakeGrant).where(IntakeGrant.id == grant_id, IntakeGrant.intake_request_id == request.id).with_for_update())
    if grant is None:
        raise HTTPException(status_code=404, detail="Convite não encontrado")
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=request.patient_id, actor_professional_id=actor.id, event_type="grant_revoked", payload={}, occurred_at=utcnow()))


async def cancel_request(db: AsyncSession, *, request: IntakeRequest, actor: Professional) -> IntakeRequest:
    request = await db.scalar(
        select(IntakeRequest)
        .where(
            IntakeRequest.id == request.id,
            IntakeRequest.owner_professional_id == actor.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Pré-atendimento não encontrado")
    if request.status not in (INTAKE_REVIEWED, INTAKE_CANCELLED):
        request.status = INTAKE_CANCELLED
        request.cancelled_at = utcnow()
        grants = (await db.execute(select(IntakeGrant).where(IntakeGrant.intake_request_id == request.id, IntakeGrant.revoked_at.is_(None)).with_for_update())).scalars().all()
        for grant in grants:
            grant.revoked_at = utcnow()
        db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=request.patient_id, actor_professional_id=actor.id, event_type="cancelled", payload={}, occurred_at=utcnow()))
        await db.flush()
    return request


@dataclass
class PublicContext:
    request: IntakeRequest
    grant: IntakeGrant


async def resolve_public(db: AsyncSession, raw_token: str | None, *, lock: bool = False) -> PublicContext:
    if not raw_token or len(raw_token) > 128:
        raise _gone()
    query = select(IntakeGrant, IntakeRequest).join(IntakeRequest, IntakeRequest.id == IntakeGrant.intake_request_id).where(IntakeGrant.token_hash == hash_token(raw_token), IntakeGrant.revoked_at.is_(None))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await db.execute(query)).first()
    if row is None:
        raise _gone()
    grant, request = row
    caregiver = await db.scalar(select(Caregiver).where(Caregiver.id == request.caregiver_id, Caregiver.patient_id == request.patient_id)) if request.caregiver_id else None
    if request.status == INTAKE_CANCELLED or caregiver is None or _as_utc(grant.expires_at) <= utcnow() or grant.caregiver_id != caregiver.id:
        raise _gone()
    return PublicContext(request=request, grant=grant)


def public_response(ctx: PublicContext, files: list[IntakeFile], *, patient: Patient, owner: Professional, can_respond: bool) -> IntakePublicResponse:
    return IntakePublicResponse(
        patient_first_name=patient.name.strip().split(" ", 1)[0],
        professional_name=owner.name,
        can_respond=can_respond and ctx.request.status == INTAKE_DRAFT and _as_utc(ctx.grant.expires_at) > utcnow(),
        form_version=ctx.request.form_version, status=ctx.request.status, responses=ctx.request.responses,
        version=ctx.request.version, expires_at=ctx.grant.expires_at, files=[_file_response(f) for f in files],
    )


async def upload_file(db: AsyncSession, *, ctx: PublicContext, raw_token: str, upload: UploadFile) -> IntakeFile:
    await _workflow_enabled(db, ctx.request.owner_professional_id)
    if ctx.request.status != INTAKE_DRAFT:
        raise _conflict("Arquivos só podem ser enviados antes do envio.")
    total = await db.scalar(select(func.count(IntakeFile.id)).where(IntakeFile.intake_request_id == ctx.request.id, IntakeFile.deleted_at.is_(None)))
    if total >= MAX_FILES:
        raise HTTPException(status_code=400, detail="O pré-atendimento aceita até 5 arquivos.")
    body = await upload.read(MAX_FILE_BYTES + 1)
    if len(body) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Cada arquivo pode ter no máximo 5 MB.")
    content_type = normalize_content_type(upload.content_type)
    if content_type not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=400, detail="Envie um PDF, JPEG ou PNG.")
    assert_declared_matches_sniff(content_type, body, {"application/pdf": frozenset({"application/pdf"}), "image/jpeg": frozenset({"image/jpeg"}), "image/png": frozenset({"image/png"})})
    name = sanitize_filename(upload.filename)
    file_id = uuid4()
    key = f"intake/{ctx.request.patient_id}/{ctx.request.id}/{file_id}/{name}"
    reservation = await reserve_storage_cleanup(db, key, reason="intake_file")
    try:
        await storage_service.upload(key, body, content_type)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Não foi possível guardar o arquivo. Tente novamente.") from exc
    # The reservation commit releases the request/grant locks. Re-resolve both
    # rows after object storage I/O so revoke/submit and parallel uploads win
    # before this blob becomes reachable from the database.
    ctx = await resolve_public(db, raw_token, lock=True)
    await _workflow_enabled(db, ctx.request.owner_professional_id)
    if ctx.request.status != INTAKE_DRAFT:
        raise _conflict("Arquivos só podem ser enviados antes do envio.")
    total = await db.scalar(select(func.count(IntakeFile.id)).where(IntakeFile.intake_request_id == ctx.request.id, IntakeFile.deleted_at.is_(None)))
    if total >= MAX_FILES:
        raise HTTPException(status_code=400, detail="O pré-atendimento aceita até 5 arquivos.")
    item = IntakeFile(id=file_id, intake_request_id=ctx.request.id, caregiver_id=ctx.request.caregiver_id, name=name, content_type=content_type, size_bytes=len(body), storage_key=key)
    db.add(item)
    resolve_storage_cleanup(reservation)
    await db.flush()
    return item


async def delete_file(db: AsyncSession, *, ctx: PublicContext, file_id: UUID) -> None:
    if ctx.request.status != INTAKE_DRAFT:
        raise _conflict("Arquivos enviados não podem ser excluídos.")
    item = await db.scalar(select(IntakeFile).where(IntakeFile.id == file_id, IntakeFile.intake_request_id == ctx.request.id, IntakeFile.deleted_at.is_(None)).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    item.deleted_at = utcnow()
    queue_storage_cleanup(db, item.storage_key, reason="intake_file_deleted")


async def review(db: AsyncSession, *, request: IntakeRequest, actor: Professional, body: IntakeReview) -> IntakeRequest:
    await _workflow_enabled(db, actor.id)
    # Lock order is patient -> intake request, matching anamnese writers.
    patient = await db.scalar(select(Patient).where(Patient.id == request.patient_id).with_for_update().execution_options(populate_existing=True))
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    request = await require_owned_request(db, request.patient_id, request.id, actor, lock=True)
    review_payload = {"selectedFields": sorted(body.selected_fields), "selectedFileIds": sorted(str(item) for item in body.selected_file_ids), "anamneseFingerprint": body.anamnese_fingerprint}
    review_hash = _payload_hash(review_payload)
    if request.status == INTAKE_REVIEWED and request.review_command_key == body.command_key:
        if request.review_payload_hash != review_hash:
            raise _conflict("A mesma chave de revisão foi usada com outra seleção.")
        return request
    if request.status != INTAKE_SUBMITTED:
        raise _conflict("Somente pré-atendimentos enviados podem ser revisados.")
    if request.version != body.expected_version:
        raise _conflict()
    if any(key not in INTAKE_FORM_KEYS for key in body.selected_fields):
        raise HTTPException(status_code=422, detail="Campo de pré-atendimento inválido.")
    current = await anamnese_service.list_entries(db, patient.id)
    current_hash = _fingerprint(current)
    if body.selected_fields and body.anamnese_fingerprint is None:
        raise _conflict("Confira a versão atual da anamnese antes de incorporar campos.")
    if body.anamnese_fingerprint is not None and body.anamnese_fingerprint != current_hash:
        raise _conflict("A anamnese mudou. Confira os dados antes de incorporar.")
    for key in body.selected_fields:
        answer = request.responses.get(key) or {}
        value = "Não sei informar" if answer.get("notKnown") else (answer.get("value") or "")
        if patient.anamnese_status != "completed":
            await anamnese_service.upsert_entries(db, patient_id=patient.id, entries=[AnamneseEntryInput(section=INTAKE_SECTIONS[key], value=value)])
    for file_id in body.selected_file_ids:
        item = await db.scalar(select(IntakeFile).where(IntakeFile.id == file_id, IntakeFile.intake_request_id == request.id, IntakeFile.deleted_at.is_(None)).with_for_update())
        if item is None:
            raise HTTPException(status_code=422, detail="Arquivo selecionado não encontrado.")
        if item.incorporated_attachment_id is None:
            attachment = Attachment(patient_id=patient.id, professional_id=actor.id, name=item.name, category="relatorio", size_bytes=item.size_bytes, storage_key=item.storage_key, date=utcnow())
            db.add(attachment)
            await db.flush()
            item.incorporated_attachment_id = attachment.id
    request.status = INTAKE_REVIEWED
    request.reviewed_at = utcnow()
    request.reviewed_by_professional_id = actor.id
    request.review_command_key = body.command_key
    request.review_payload_hash = review_hash
    request.anamnese_fingerprint = await anamnese_fingerprint(db, patient.id)
    # The private projection labels these as incorporated fields. A completed
    # anamnese is immutable, so selected family fields remain audit-only.
    request.selected_fields = list(body.selected_fields) if patient.anamnese_status != "completed" else []
    request.selected_file_ids = [str(item) for item in body.selected_file_ids]
    request.version += 1
    db.add(IntakeAuditEvent(intake_request_id=request.id, patient_id=patient.id, actor_professional_id=actor.id, event_type="reviewed", payload={"selectedFields": body.selected_fields, "selectedFileIds": [str(item) for item in body.selected_file_ids], "anamneseFingerprint": request.anamnese_fingerprint, "anamneseImported": patient.anamnese_status != "completed"}, occurred_at=utcnow()))
    await db.flush()
    return request


async def invalidate_intake_grants(db: AsyncSession, patient_id: UUID, caregiver_id: UUID | None = None) -> None:
    query = select(IntakeGrant).join(IntakeRequest).where(IntakeRequest.patient_id == patient_id, IntakeGrant.revoked_at.is_(None))
    if caregiver_id:
        query = query.where(IntakeGrant.caregiver_id == caregiver_id)
    for grant in (await db.execute(query.with_for_update())).scalars().all():
        grant.revoked_at = utcnow()


def token_url(raw: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/pre-atendimento#token={quote(raw)}"
