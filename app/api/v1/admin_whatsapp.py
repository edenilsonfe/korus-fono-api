"""Admin endpoints for the platform WhatsApp connection (welcome messages)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import PERMISSION_MESSAGING_READ, PERMISSION_MESSAGING_WRITE
from app.core.config import get_settings
from app.core.deps import require_admin_permission
from app.constants.whatsapp_events import DEFAULT_WELCOME_MESSAGE
from app.db.session import get_db
from app.models.professional import Professional
from app.models.whatsapp_connection import CONNECTION_STATUS_ACTIVE, CONNECTION_STATUS_NOT_CONNECTED
from app.schemas.admin_whatsapp import (
    WhatsAppWelcomeMessageResponse,
    WhatsAppWelcomeMessageUpdate,
)
from app.schemas.whatsapp import (
    WhatsAppConnectResponse,
    WhatsAppConnectionStatus,
    WhatsAppStatusResponse,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.platform_whatsapp_service import PlatformWhatsAppService

router = APIRouter(prefix="/admin/whatsapp/platform", tags=["admin-whatsapp"])


def _connection_schema(
    connection,
    *,
    qrcode_base64: str | None = None,
    connection_state: str | None = None,
) -> WhatsAppConnectionStatus:
    if not connection:
        return WhatsAppConnectionStatus(status=CONNECTION_STATUS_NOT_CONNECTED)
    return WhatsAppConnectionStatus(
        status=connection.status,
        display_phone_number=connection.display_phone_number,
        last_error=connection.last_error,
        connected_at=connection.connected_at,
        evolution_instance_name=connection.evolution_instance_name,
        qrcode_base64=qrcode_base64,
        connection_state=connection_state,
    )


def _status_response(connection, can_send: bool) -> WhatsAppStatusResponse:
    return WhatsAppStatusResponse(
        provider=get_settings().whatsapp_provider,
        connection=_connection_schema(connection),
        can_send=can_send,
    )


@router.get("", response_model=WhatsAppStatusResponse)
async def get_platform_whatsapp_status(
    _: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = PlatformWhatsAppService(db)
    connection = await service.get_connection()
    connection = await service.reconcile_status(connection)
    return _status_response(connection, await service.can_send(connection))


@router.post("/connect", response_model=WhatsAppConnectResponse)
async def connect_platform_whatsapp(
    actor: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    if get_settings().whatsapp_provider != "evolution":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este endpoint é exclusivo do provider Evolution.",
        )
    service = PlatformWhatsAppService(db)
    result = await service.connect()
    await AdminAuditService(db).log(actor=actor, action="connect_platform_whatsapp")
    await db.commit()
    return WhatsAppConnectResponse(
        provider="evolution",
        connection=_connection_schema(
            result.connection,
            qrcode_base64=result.qrcode_base64,
            connection_state=result.connection_state,
        ),
        qrcode_base64=result.qrcode_base64,
        connection_state=result.connection_state,
        can_send=result.connection.status == CONNECTION_STATUS_ACTIVE,
    )


@router.post("/refresh-connection", response_model=WhatsAppConnectResponse)
async def refresh_platform_whatsapp(
    actor: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = PlatformWhatsAppService(db)
    result = await service.refresh_connection()
    await AdminAuditService(db).log(actor=actor, action="refresh_platform_whatsapp")
    await db.commit()
    return WhatsAppConnectResponse(
        provider="evolution",
        connection=_connection_schema(
            result.connection,
            qrcode_base64=result.qrcode_base64,
            connection_state=result.connection_state,
        ),
        qrcode_base64=result.qrcode_base64,
        connection_state=result.connection_state,
        can_send=await service.can_send(result.connection),
    )


@router.post("/disconnect", response_model=WhatsAppStatusResponse)
async def disconnect_platform_whatsapp(
    actor: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = PlatformWhatsAppService(db)
    connection = await service.disconnect()
    await AdminAuditService(db).log(actor=actor, action="disconnect_platform_whatsapp")
    await db.commit()
    return _status_response(connection, False)


@router.get("/message", response_model=WhatsAppWelcomeMessageResponse)
async def get_platform_whatsapp_message(
    _: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_READ)),
    db: AsyncSession = Depends(get_db),
):
    service = PlatformWhatsAppService(db)
    connection = await service.get_connection()
    return WhatsAppWelcomeMessageResponse(
        message=connection.welcome_message,
        default_message=DEFAULT_WELCOME_MESSAGE,
    )


@router.put("/message", response_model=WhatsAppWelcomeMessageResponse)
async def update_platform_whatsapp_message(
    body: WhatsAppWelcomeMessageUpdate,
    actor: Professional = Depends(require_admin_permission(PERMISSION_MESSAGING_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    service = PlatformWhatsAppService(db)
    connection = await service.get_connection()
    await service.set_welcome_message(connection, body.message)
    await AdminAuditService(db).log(
        actor=actor,
        action="update_platform_whatsapp_message",
        payload={"custom_message": bool(body.message and body.message.strip())},
    )
    await db.commit()
    return WhatsAppWelcomeMessageResponse(
        message=connection.welcome_message,
        default_message=DEFAULT_WELCOME_MESSAGE,
    )
