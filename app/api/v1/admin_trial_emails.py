"""Staff-only trial email campaigns."""

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_MESSAGING_READ, PERMISSION_MESSAGING_WRITE
from app.core.deps import require_admin_permission
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.admin_trial_email import (
    TrialEmailAudience,
    TrialEmailCampaignCreate,
    TrialEmailCampaignResponse,
    TrialEmailPreview,
)
from app.services.trial_email_campaign_service import TrialEmailCampaignService
from app.services.trial_email_queue import enqueue_trial_email_campaign

router = APIRouter(prefix="/admin/trial-emails", tags=["admin-trial-emails"])


@router.get("/preview", response_model=TrialEmailPreview)
async def preview_trial_email_campaign(
    audience: TrialEmailAudience,
    expires_within_days: int = Query(3, ge=1, le=30, alias="expiresWithinDays"),
    _: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await TrialEmailCampaignService(db).preview(
        audience,
        None if audience == "expired" else expires_within_days,
    )


@router.get("/campaigns", response_model=list[TrialEmailCampaignResponse])
async def list_trial_email_campaigns(
    limit: int = Query(20, ge=1, le=100),
    _: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_READ)),
    db: AsyncSession = Depends(get_db),
):
    return await TrialEmailCampaignService(db).list_campaigns(limit)


@router.post(
    "/campaigns",
    response_model=TrialEmailCampaignResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_trial_email_campaign(
    body: TrialEmailCampaignCreate,
    background_tasks: BackgroundTasks,
    actor: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    campaign = await TrialEmailCampaignService(db).create_campaign(
        actor=actor,
        audience=body.audience,
        expires_within_days=body.expires_within_days,
    )
    if campaign.status == "pending":
        background_tasks.add_task(enqueue_trial_email_campaign, campaign.id)
    return campaign
