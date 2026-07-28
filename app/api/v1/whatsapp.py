from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.whatsapp_events import DEFAULT_EVENT_MESSAGE_TEMPLATES
from app.core.config import get_settings
from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.professional import Professional
from app.models.whatsapp_connection import CONNECTION_STATUS_NOT_CONNECTED, WhatsAppConnection
from app.schemas.whatsapp import (
    WhatsAppConnectResponse,
    WhatsAppConnectionStatus,
    WhatsAppStatusResponse,
    WhatsAppUsageResponse,
)
from app.schemas.whatsapp_message_logs import (
    WhatsAppMessageLogsResponse,
    WhatsAppMessageLogsStatsResponse,
)
from app.schemas.whatsapp_settings import (
    WhatsAppEventSettings,
    WhatsAppSettingsResponse,
    WhatsAppSettingsUpdate,
)
from app.services.evolution_whatsapp_service import EvolutionWhatsAppService
from app.services.notification_settings_service import NotificationSettingsService
from app.services.whatsapp_message_log_service import WhatsAppMessageLogService
from app.services.whatsapp_provider import get_active_whatsapp_provider, whatsapp_can_send

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def _connection_schema(
    connection: WhatsAppConnection | None,
    *,
    qrcode_base64: str | None = None,
    connection_state: str | None = None,
) -> WhatsAppConnectionStatus:
    if not connection:
        return WhatsAppConnectionStatus(status=CONNECTION_STATUS_NOT_CONNECTED)
    return WhatsAppConnectionStatus(
        status=connection.status,
        waba_id=connection.waba_id,
        phone_number_id=connection.phone_number_id,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        last_error=connection.last_error,
        connected_at=connection.connected_at,
        evolution_instance_name=connection.evolution_instance_name,
        qrcode_base64=qrcode_base64,
        connection_state=connection_state,
    )


async def _build_evolution_status(
    service: EvolutionWhatsAppService, professional: Professional
) -> WhatsAppStatusResponse:
    connection = await service.get_active_connection(professional.id)
    return WhatsAppStatusResponse(
        provider=get_settings().whatsapp_provider,
        embedded_signup_enabled=False,
        connection=_connection_schema(connection),
        can_send=await service.can_send(professional.id),
    )


def _settings_response(settings) -> WhatsAppSettingsResponse:
    stored_templates = settings.whatsapp_message_templates or {}
    return WhatsAppSettingsResponse(
        whatsapp_enabled=settings.whatsapp_enabled,
        whatsapp_events=WhatsAppEventSettings.from_dict(settings.whatsapp_events),
        whatsapp_message_templates={
            key: stored_templates.get(key) for key in DEFAULT_EVENT_MESSAGE_TEMPLATES
        },
        template_defaults=dict(DEFAULT_EVENT_MESSAGE_TEMPLATES),
    )


@router.get("/status", response_model=WhatsAppStatusResponse)
async def get_whatsapp_status(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if get_settings().whatsapp_provider == "evolution":
        return await _build_evolution_status(EvolutionWhatsAppService(db), professional)
    return WhatsAppStatusResponse(
        provider=get_settings().whatsapp_provider,
        embedded_signup_enabled=False,
        connection=WhatsAppConnectionStatus(status=CONNECTION_STATUS_NOT_CONNECTED),
        can_send=await whatsapp_can_send(db, professional.id),
    )


@router.post("/connect", response_model=WhatsAppConnectResponse)
async def connect_evolution_whatsapp(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if get_settings().whatsapp_provider != "evolution":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este endpoint é exclusivo do provider Evolution.",
        )
    service = EvolutionWhatsAppService(db)
    result = await service.connect(professional.id)
    return WhatsAppConnectResponse(
        provider="evolution",
        connection=_connection_schema(
            result.connection,
            qrcode_base64=result.qrcode_base64,
            connection_state=result.connection_state,
        ),
        qrcode_base64=result.qrcode_base64,
        connection_state=result.connection_state,
        can_send=await service.can_send(professional.id),
    )


@router.post("/refresh-connection", response_model=WhatsAppStatusResponse)
async def refresh_evolution_connection(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if get_settings().whatsapp_provider != "evolution":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este endpoint é exclusivo do provider Evolution.",
        )
    service = EvolutionWhatsAppService(db)
    await service.refresh_connection(professional.id)
    return await _build_evolution_status(service, professional)


@router.post("/disconnect", response_model=WhatsAppStatusResponse)
async def disconnect_whatsapp(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    provider = get_active_whatsapp_provider(db)
    await provider.disconnect(professional.id)  # type: ignore[union-attr]
    if get_settings().whatsapp_provider == "evolution":
        return await _build_evolution_status(EvolutionWhatsAppService(db), professional)
    return WhatsAppStatusResponse(
        provider=get_settings().whatsapp_provider,
        connection=WhatsAppConnectionStatus(status=CONNECTION_STATUS_NOT_CONNECTED),
        can_send=False,
    )


@router.get("/usage", response_model=WhatsAppUsageResponse)
async def get_whatsapp_usage(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    usage = await get_active_whatsapp_provider(db).get_usage(professional.id)
    return WhatsAppUsageResponse(**usage)


@router.get("/settings", response_model=WhatsAppSettingsResponse)
async def get_whatsapp_settings(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    settings = await NotificationSettingsService(db).get_or_create(professional.id)
    await db.commit()
    return _settings_response(settings)


@router.put("/settings", response_model=WhatsAppSettingsResponse)
async def update_whatsapp_settings(
    body: WhatsAppSettingsUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = NotificationSettingsService(db)
    settings = await service.update(
        professional.id,
        whatsapp_enabled=body.whatsapp_enabled,
        whatsapp_events=body.whatsapp_events.to_update_dict() if body.whatsapp_events else None,
        whatsapp_message_templates=(
            body.whatsapp_message_templates.to_update_dict()
            if body.whatsapp_message_templates
            else None
        ),
    )
    await db.commit()
    return _settings_response(settings)


@router.get("/message-logs/stats", response_model=WhatsAppMessageLogsStatsResponse)
async def get_whatsapp_message_logs_stats(
    days: int = 30,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = WhatsAppMessageLogService(db)
    return WhatsAppMessageLogsStatsResponse(
        **await service.get_stats(professional.id, days=min(max(days, 1), 90))
    )


@router.get("/message-logs", response_model=WhatsAppMessageLogsResponse)
async def list_whatsapp_message_logs(
    page: int = 1,
    page_size: int = 20,
    event_type: str | None = None,
    status: str | None = None,
    days: int = 30,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    service = WhatsAppMessageLogService(db)
    return WhatsAppMessageLogsResponse(
        **await service.list_logs(
            professional.id,
            page=page,
            page_size=page_size,
            event_type=event_type,
            status=status,
            days=min(max(days, 1), 90),
        )
    )
