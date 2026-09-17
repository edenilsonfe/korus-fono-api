import json
import logging

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.evolution_webhook_auth import verify_evolution_webhook_request
from app.services.evolution_whatsapp_service import EvolutionWhatsAppService
from app.services.platform_whatsapp_service import PlatformWhatsAppService
from app.services.weekly_summary_email_service import (
    handle_resend_weekly_summary_event,
    verify_resend_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/resend/email", status_code=status.HTTP_204_NO_CONTENT)
async def receive_resend_email_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    if not verify_resend_webhook_signature(
        body,
        message_id=request.headers.get("svix-id", ""),
        timestamp=request.headers.get("svix-timestamp", ""),
        signature_header=request.headers.get("svix-signature", ""),
    ):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if isinstance(payload, dict):
        await handle_resend_weekly_summary_event(db, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/evolution/whatsapp")
async def receive_evolution_whatsapp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    if not verify_evolution_webhook_request(request, body):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if not isinstance(payload, dict):
        return Response(status_code=status.HTTP_200_OK)

    platform_service = PlatformWhatsAppService(db)
    handled = await platform_service.handle_webhook_event(payload)
    if not handled:
        service = EvolutionWhatsAppService(db)
        await service.handle_webhook_event(payload)
    return Response(status_code=status.HTTP_200_OK)
