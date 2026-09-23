"""Private professional and token-only family routes for pre-attendance."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_patient_for_professional, require_verified_professional
from app.core.client_ip import get_client_ip
from app.db.session import get_db
from app.models.intake import IntakeFile, IntakeGrant
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.intake import (
    IntakeCreate,
    IntakeDraftPatch,
    IntakeFileResponse,
    IntakeGrantIssue,
    IntakeGrantIssuedResponse,
    IntakePublicResponse,
    IntakeRequestResponse,
    IntakeReview,
    IntakeSubmit,
)
from app.services import clinical_public_rate_limit, intake_service
from app.services.entitlement_service import EntitlementService
from app.services.feature_flag_service import FeatureFlagService

router = APIRouter(tags=["intake"])
IntakeToken = Annotated[str | None, Header(alias="X-Intake-Token")]


async def _private_request(db, patient_id, request_id, actor):
    return await intake_service.require_owned_request(db, patient_id, request_id, actor)


@router.get("/patients/{patient_id}/intake-requests", response_model=list[IntakeRequestResponse])
async def list_intake_requests(patient: Patient = Depends(get_patient_for_professional), professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return [await intake_service.request_response(db, item) for item in await intake_service.list_requests(db, patient.id, professional)]


@router.post("/patients/{patient_id}/intake-requests", response_model=IntakeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_intake_request(patient: Patient = Depends(get_patient_for_professional), body: IntakeCreate = ..., professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    item = await intake_service.create_request(db, patient=patient, actor=professional, body=body)
    return await intake_service.request_response(db, item)


@router.get("/patients/{patient_id}/intake-requests/{request_id}", response_model=IntakeRequestResponse)
async def get_intake_request(patient_id: UUID, request_id: UUID, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    item = await _private_request(db, patient_id, request_id, professional)
    return await intake_service.request_response(db, item)


@router.post("/patients/{patient_id}/intake-requests/{request_id}/grants", response_model=IntakeGrantIssuedResponse)
async def issue_intake_grant(patient_id: UUID, request_id: UUID, body: IntakeGrantIssue, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    request_item = await _private_request(db, patient_id, request_id, professional)
    grant, raw = await intake_service.issue_grant(db, request=request_item, actor=professional, body=body)
    return IntakeGrantIssuedResponse(id=str(grant.id), expires_at=grant.expires_at, revoked_at=grant.revoked_at, active=True, url=intake_service.token_url(raw))


@router.delete("/patients/{patient_id}/intake-requests/{request_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_intake_grant(patient_id: UUID, request_id: UUID, grant_id: UUID, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    request_item = await _private_request(db, patient_id, request_id, professional)
    await intake_service.revoke_grant(db, request=request_item, grant_id=grant_id, actor=professional)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/patients/{patient_id}/intake-requests/{request_id}/review", response_model=IntakeRequestResponse)
async def review_intake(patient_id: UUID, request_id: UUID, body: IntakeReview, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    request_item = await _private_request(db, patient_id, request_id, professional)
    item = await intake_service.review(db, request=request_item, actor=professional, body=body)
    return await intake_service.request_response(db, item)


@router.post("/patients/{patient_id}/intake-requests/{request_id}/cancel", response_model=IntakeRequestResponse)
async def cancel_intake(patient_id: UUID, request_id: UUID, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    request_item = await _private_request(db, patient_id, request_id, professional)
    item = await intake_service.cancel_request(db, request=request_item, actor=professional)
    return await intake_service.request_response(db, item)


@router.get("/patients/{patient_id}/intake-requests/{request_id}/files/{file_id}")
async def get_private_intake_file(patient_id: UUID, request_id: UUID, file_id: UUID, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    request_item = await _private_request(db, patient_id, request_id, professional)
    item = await db.scalar(select(IntakeFile).where(IntakeFile.id == file_id, IntakeFile.intake_request_id == request_item.id, IntakeFile.deleted_at.is_(None)))
    if item is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    body, content_type = await intake_service.storage_service.download(item.storage_key)
    return Response(content=body, media_type=content_type or item.content_type, headers={"Content-Disposition": f'inline; filename="{intake_service.safe_content_disposition_filename(item.storage_key, item.name)}"'})


async def _public_context(request: Request, raw_token: str | None, db: AsyncSession, *, write: bool = False):
    clinical_public_rate_limit.enforce_public_rate_limit(namespace="clinical:intake:ip", identifier_hash=clinical_public_rate_limit.hash_identifier(get_client_ip(request)), max_requests=120, window_seconds=60, detail="Muitas solicitações ao pré-atendimento. Tente novamente em instantes.", endpoint="intake")
    context = await intake_service.resolve_public(db, raw_token, lock=write)
    clinical_public_rate_limit.enforce_public_rate_limit(namespace="clinical:intake:token", identifier_hash=clinical_public_rate_limit.hash_identifier(raw_token or ""), max_requests=60, window_seconds=60, detail="Muitas solicitações ao pré-atendimento. Tente novamente em instantes.", endpoint="intake")
    if write:
        await intake_service._workflow_enabled(db, context.request.owner_professional_id)
    return context


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _public_payload(db: AsyncSession, context):
    files = (await db.execute(select(IntakeFile).where(IntakeFile.intake_request_id == context.request.id, IntakeFile.deleted_at.is_(None)).order_by(IntakeFile.created_at))).scalars().all()
    patient = await db.get(Patient, context.request.patient_id)
    owner = await db.get(Professional, context.request.owner_professional_id)
    can_respond = await EntitlementService(db).can_write(owner) and await FeatureFlagService(db).is_enabled(owner, "patient_intake")
    return intake_service.public_response(context, files, patient=patient, owner=owner, can_respond=can_respond)


@router.get("/intake-responses", response_model=IntakePublicResponse)
async def read_intake_response(request: Request, response: Response, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db)
    _no_store(response)
    return await _public_payload(db, context)


@router.patch("/intake-responses", response_model=IntakePublicResponse)
async def save_intake_response(request: Request, response: Response, body: IntakeDraftPatch, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db, write=True)
    await intake_service.save_draft(db, raw_token=x_intake_token or "", request=context.request, body=body)
    _no_store(response)
    return await _public_payload(db, context)


@router.post("/intake-responses/submit", response_model=IntakePublicResponse)
async def submit_intake_response(request: Request, response: Response, body: IntakeSubmit, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db, write=True)
    await intake_service.submit(db, request=context.request, body=body, raw_token=x_intake_token or "")
    _no_store(response)
    return await _public_payload(db, context)


@router.post("/intake-responses/files", response_model=IntakeFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_intake_file(request: Request, response: Response, file: UploadFile = File(...), x_intake_token: IntakeToken = None, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db, write=True)
    item = await intake_service.upload_file(db, ctx=context, raw_token=x_intake_token or "", upload=file)
    _no_store(response)
    return intake_service._file_response(item)


@router.get("/intake-responses/files", response_model=list[IntakeFileResponse])
async def list_intake_files(request: Request, response: Response, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db)
    _no_store(response)
    return [intake_service._file_response(item) for item in (await db.execute(select(IntakeFile).where(IntakeFile.intake_request_id == context.request.id, IntakeFile.deleted_at.is_(None)).order_by(IntakeFile.created_at))).scalars().all()]


@router.delete("/intake-responses/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intake_file(request: Request, response: Response, file_id: UUID, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db, write=True)
    await intake_service.delete_file(db, ctx=context, file_id=file_id)
    _no_store(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.get("/intake-responses/files/{file_id}/file")
async def get_intake_file(request: Request, file_id: UUID, x_intake_token: IntakeToken, db: AsyncSession = Depends(get_db)):
    context = await _public_context(request, x_intake_token, db)
    item = await db.scalar(select(IntakeFile).where(IntakeFile.id == file_id, IntakeFile.intake_request_id == context.request.id, IntakeFile.deleted_at.is_(None)))
    if item is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    body, content_type = await intake_service.storage_service.download(item.storage_key)
    return Response(content=body, media_type=content_type or item.content_type, headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{intake_service.safe_content_disposition_filename(item.storage_key, item.name)}"'})
