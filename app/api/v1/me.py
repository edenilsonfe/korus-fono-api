from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import resolve_admin_role
from app.core.deps import admin_permissions_for, get_current_professional, require_verified_professional
from app.core.specialty_catalog import specialty_label
from app.db.session import get_db
from app.models.professional import Professional
from app.schemas.professional import (
    AnalyticsConsentUpdate,
    BrandingAssetsResponse,
    ProfessionalResponse,
    ProfessionalUpdate,
)
from app.services.analytics_consent import has_analytics_consent, set_analytics_consent
from app.schemas.onboarding import OnboardingResponse, OnboardingUpdate
from app.services.onboarding_service import build_onboarding_response, update_onboarding
from app.services.billing_profile_service import billing_profile_is_complete
from app.services.professional_branding import (
    branding_urls,
    delete_branding_image,
    store_branding_image,
)

router = APIRouter(prefix="/me", tags=["me"])


def _to_response(p: Professional) -> ProfessionalResponse:
    return ProfessionalResponse(
        analytics_consent=has_analytics_consent(p),
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
        temporary_access_ends_at=p.temporary_access_ends_at,
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


@router.patch("/analytics-consent", response_model=AnalyticsConsentUpdate)
async def update_analytics_consent(
    body: AnalyticsConsentUpdate,
    professional: Professional = Depends(get_current_professional),
    db: AsyncSession = Depends(get_db),
):
    set_analytics_consent(professional, body.analytics_consent)
    await db.commit()
    return body


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
    return await update_onboarding(db, professional, body.action, report_id=body.report_id)


@router.get("/branding", response_model=BrandingAssetsResponse)
async def get_branding(
    professional: Professional = Depends(require_verified_professional),
):
    logo_url, signature_url = await branding_urls(professional)
    return BrandingAssetsResponse(logo_url=logo_url, signature_url=signature_url)


@router.post("/branding/{asset}", response_model=BrandingAssetsResponse)
async def upload_branding_asset(
    asset: Literal["logo", "signature"],
    file: UploadFile = File(...),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    body = await file.read()
    await store_branding_image(
        db, professional, asset, content_type=file.content_type, body=body
    )
    await db.commit()
    logo_url, signature_url = await branding_urls(professional)
    return BrandingAssetsResponse(logo_url=logo_url, signature_url=signature_url)


@router.delete("/branding/{asset}", response_model=BrandingAssetsResponse)
async def remove_branding_asset(
    asset: Literal["logo", "signature"],
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await delete_branding_image(db, professional, asset)
    await db.commit()
    logo_url, signature_url = await branding_urls(professional)
    return BrandingAssetsResponse(logo_url=logo_url, signature_url=signature_url)
