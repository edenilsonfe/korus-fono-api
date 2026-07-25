from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_professional, require_verified_professional
from app.core.specialty_catalog import specialty_label
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.professional import ProfessionalResponse, ProfessionalUpdate

router = APIRouter(prefix="/me", tags=["me"])


def _to_response(p: Professional) -> ProfessionalResponse:
    return ProfessionalResponse(
        id=str(p.id),
        name=p.name,
        specialty=p.specialty or specialty_label(p.specialty_key),
        specialty_key=p.specialty_key,
        council=p.council,
        email=p.email,
        phone=p.phone,
        cpf=p.cpf or "",
        avatar_color=p.avatar_color,
        is_staff=p.is_staff,
        email_verified=p.email_verified_at is not None,
    )


@router.get("", response_model=ProfessionalResponse)
async def get_me(professional: Professional = Depends(get_current_professional)):
    return _to_response(professional)


@router.patch("", response_model=ProfessionalResponse)
async def update_me(
    body: ProfessionalUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    data = body.model_dump(exclude_unset=True)
    if "specialty_key" in data and data["specialty_key"] is not None:
        professional.specialty_key = data.pop("specialty_key")
        professional.specialty = specialty_label(professional.specialty_key)
    for field, value in data.items():
        setattr(professional, field, value)
    await db.flush()
    return _to_response(professional)
