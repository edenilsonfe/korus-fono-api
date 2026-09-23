from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.models.patient import Patient
from app.schemas.ai import AIReportResponse
from app.schemas.clinical_review import (
    ClinicalReviewCancel,
    ClinicalReviewComplete,
    ClinicalReviewCreate,
    ClinicalReviewResponse,
    ClinicalReviewSourcePage,
    ClinicalReviewUpdate,
    DischargePreview,
    DueClinicalReviewResponse,
    FamilyReportCreate,
)
from app.schemas.common import PaginatedResponse
from app.services import clinical_review_service
from app.services.google_calendar_service import dispatch_sync_records

router = APIRouter(prefix="/patients", tags=["clinical-reviews"])
due_router = APIRouter(prefix="/clinical-reviews", tags=["clinical-reviews"])

VerifiedProfessional = Annotated[Professional, Depends(require_verified_professional)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


def _response(review) -> ClinicalReviewResponse:
    return ClinicalReviewResponse.model_validate(clinical_review_service._review_response_data(review))


@router.get("/{patient_id}/clinical-reviews", response_model=PaginatedResponse[ClinicalReviewResponse])
async def list_clinical_reviews(
    patient_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    status_filter: Annotated[Literal["draft", "completed", "cancelled"] | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    rows, total = await clinical_review_service.list_reviews(db, patient_id, professional, page=page, limit=limit, status_filter=status_filter)
    return PaginatedResponse(items=[_response(row) for row in rows], total=total, page=page, limit=limit)


@router.post("/{patient_id}/clinical-reviews", response_model=ClinicalReviewResponse, status_code=status.HTTP_201_CREATED)
async def create_clinical_review(
    patient_id: UUID,
    body: ClinicalReviewCreate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    review = await clinical_review_service.create_review(db, patient_id, professional, body)
    await db.commit()
    await db.refresh(review)
    return _response(review)


@router.get("/{patient_id}/clinical-reviews/{review_id}", response_model=ClinicalReviewResponse)
async def get_clinical_review(patient_id: UUID, review_id: UUID, professional: VerifiedProfessional, db: DatabaseSession):
    return _response(await clinical_review_service.get_review(db, patient_id, review_id, professional))


@router.patch("/{patient_id}/clinical-reviews/{review_id}", response_model=ClinicalReviewResponse)
async def update_clinical_review(
    patient_id: UUID,
    review_id: UUID,
    body: ClinicalReviewUpdate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    review = await clinical_review_service.update_review(db, patient_id, review_id, professional, body)
    await db.commit()
    await db.refresh(review)
    return _response(review)


@router.get("/{patient_id}/clinical-review-sources", response_model=ClinicalReviewSourcePage)
async def list_clinical_review_sources(
    patient_id: UUID,
    professional: VerifiedProfessional,
    db: DatabaseSession,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    kind: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    rows, total = await clinical_review_service.list_sources(db, patient_id, professional, from_date=from_date, to_date=to_date, kind=kind, page=page, limit=limit)
    return ClinicalReviewSourcePage(items=rows, total=total, page=page, limit=limit)


@router.post("/{patient_id}/clinical-reviews/{review_id}/complete", response_model=ClinicalReviewResponse)
async def complete_clinical_review(
    patient_id: UUID,
    review_id: UUID,
    body: ClinicalReviewComplete,
    background_tasks: BackgroundTasks,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    review = await clinical_review_service.complete_review(db, patient_id, review_id, professional, body)
    queued_ids = getattr(review, "_queued_google_record_ids", [])
    await db.commit()
    if queued_ids:
        background_tasks.add_task(dispatch_sync_records, queued_ids)
    await db.refresh(review)
    return _response(review)


@router.post("/{patient_id}/clinical-reviews/{review_id}/cancel", response_model=ClinicalReviewResponse)
async def cancel_clinical_review(
    patient_id: UUID,
    review_id: UUID,
    body: ClinicalReviewCancel,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    review = await clinical_review_service.cancel_review(db, patient_id, review_id, professional, body.expected_version)
    await db.commit()
    await db.refresh(review)
    return _response(review)


@router.get("/{patient_id}/clinical-reviews/{review_id}/discharge-preview", response_model=DischargePreview)
async def get_discharge_preview(patient_id: UUID, review_id: UUID, professional: VerifiedProfessional, db: DatabaseSession):
    return await clinical_review_service.discharge_preview(db, patient_id, review_id, professional)


@router.post("/{patient_id}/clinical-reviews/{review_id}/family-report", response_model=AIReportResponse)
async def create_clinical_review_family_report(
    patient_id: UUID,
    review_id: UUID,
    body: FamilyReportCreate,
    professional: VerifiedProfessional,
    db: DatabaseSession,
):
    report = await clinical_review_service.create_family_report(db, patient_id, review_id, professional, body)
    patient_name = await db.scalar(select(Patient.name).where(Patient.id == patient_id))
    await db.commit()
    return AIReportResponse(
        id=str(report.id), type=report.type, patient_id=str(report.patient_id), patient=str(patient_name or ""),
        date=report.date.isoformat(), preview=report.preview, content=report.content, status=report.status,
        version=report.version, composition_id=None,
    )


@due_router.get("/due", response_model=list[DueClinicalReviewResponse])
async def list_due_clinical_reviews(professional: VerifiedProfessional, db: DatabaseSession):
    return await clinical_review_service.list_due(db, professional)
