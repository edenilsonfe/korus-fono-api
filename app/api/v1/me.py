from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import resolve_admin_role
from app.core.deps import admin_permissions_for, get_current_professional, require_verified_professional
from app.core.specialty_catalog import specialty_label
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.professional import ProfessionalResponse, ProfessionalUpdate
from app.schemas.onboarding import OnboardingResponse, OnboardingUpdate
from app.services.onboarding_service import build_onboarding_response, update_onboarding
from app.services.billing_profile_service import billing_profile_is_complete

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
        billing_address=p.billing_address,
        billing_address_number=p.billing_address_number,
        billing_address_complement=p.billing_address_complement,
        billing_province=p.billing_province,
        billing_postal_code=p.billing_postal_code,
        billing_profile_complete=billing_profile_is_complete(p),
        avatar_color=p.avatar_color,
        is_staff=p.is_staff,
        admin_role=resolve_admin_role(admin_role=p.admin_role, is_staff=p.is_staff),
        admin_permissions=admin_permissions_for(p),
        email_verified=p.email_verified_at is not None,
        signup_payment_required=p.signup_payment_required,
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


@router.get("/activation", response_model=OnboardingResponse)
async def get_activation(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await build_onboarding_response(db, professional)


@router.patch("/activation", response_model=OnboardingResponse)
async def patch_activation(
    body: OnboardingUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await update_onboarding(db, professional, body.action)
