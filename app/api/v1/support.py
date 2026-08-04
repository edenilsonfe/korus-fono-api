"""Canal de suporte: form de contato enviado por e-mail à equipe."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import require_verified_professional
from app.models.professional import Professional
from app.schemas.support import SupportContactRequest, SupportContactResponse
from app.services.support_contact import send_support_contact_sync

router = APIRouter(prefix="/support", tags=["support"])


@router.post("/contact", response_model=SupportContactResponse)
async def send_contact(
    payload: SupportContactRequest,
    professional: Professional = Depends(require_verified_professional),
):
    """Envia form de contato ao e-mail da equipe (remetente = sessão)."""
    if not (get_settings().support_contact_email or "").strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Canal de e-mail do suporte não configurado",
        )
    await asyncio.to_thread(
        send_support_contact_sync,
        professional_name=professional.name,
        professional_email=professional.email,
        subject=payload.subject,
        message=payload.message,
    )
    return SupportContactResponse(
        message="Mensagem enviada. Retornaremos em até 1 dia útil."
    )
