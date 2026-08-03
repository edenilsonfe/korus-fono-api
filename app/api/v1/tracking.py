"""Meta Pixel: config pública para o snippet + reenvio server-side (CAPI)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import get_client_ip
from app.core.deps import get_current_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.common import MessageResponse
from app.schemas.marketing import PixelConfigResponse, TrackEventRequest
from app.services.meta_pixel_service import MetaPixelService

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/pixel-config", response_model=PixelConfigResponse)
async def get_pixel_config() -> PixelConfigResponse:
    """Config pública para instalar o snippet do Pixel no web (sem auth)."""
    service = MetaPixelService()
    return PixelConfigResponse(pixel_id=service.pixel_id, enabled=service.enabled)


@router.post("/events", response_model=MessageResponse)
async def forward_tracking_event(
    body: TrackEventRequest,
    request: Request,
    professional: Professional = Depends(get_current_professional),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Reenvia evento do navegador ao Conversions API (dedup por event_id).

    Útil para eventos que o snippet dispara client-side (ex.: ViewContent,
    PageView) e que devem chegar também via CAPI sem expor o access token.
    """
    service = MetaPixelService()
    user_data = service.build_user_data(
        email=professional.email,
        first_name=professional.name,
        client_ip=get_client_ip(request),
        client_user_agent=request.headers.get("user-agent"),
        fbp=body.fbp,
        fbc=body.fbc,
    )
    await service.send_event(
        event_name=body.event_name,
        event_id=body.event_id,
        event_source_url=body.event_source_url,
        custom_data=body.custom_data,
        user_data=user_data,
    )
    return MessageResponse(message="Evento rastreado")
