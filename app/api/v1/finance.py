"""HTTP contract for internal clinic finance (not SaaS billing)."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.db.session import get_db
from app.models.finance import (
    FinancialCategory,
    FinancialPayment,
    FinancialProfile,
    PaymentMethod,
)
from app.models.professional import Professional
from app.schemas.finance import (
    CancellationRequest,
    CashFlowResponse,
    FinanceDashboardResponse,
    FinancialProfileResponse,
    FinancialProfileUpdate,
    NamedConfigCreate,
    NamedConfigResponse,
    NamedConfigUpdate,
    PackageCreate,
    PackageResponse,
    PackageUpdate,
    PatientFinanceResponse,
    PatientPackageCreate,
    PatientPackageResponse,
    PayableCreate,
    PayableListResponse,
    PayableResponse,
    PaymentCreate,
    PaymentResponse,
    ReceivableCreate,
    ReceivableListResponse,
    ReceivableResponse,
    ServiceCreate,
    ServiceResponse,
    ServiceUpdate,
    SettlementCreate,
    SettlementResponse,
)
from app.services import financial_service as service
from app.services.financial_defaults import (
    ensure_default_financial_categories,
    ensure_default_payment_methods,
)
from app.services.financial_receipt_service import build_internal_receipt

router = APIRouter(prefix="/finance", tags=["finance"])
patient_router = APIRouter(prefix="/patients/{patient_id}/finance", tags=["finance"])


@router.get("/profile", response_model=FinancialProfileResponse)
async def get_profile(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_or_create_profile(db, professional)


@router.put("/profile", response_model=FinancialProfileResponse)
async def put_profile(
    body: FinancialProfileUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_profile(db, professional, body)


@router.get("/categories", response_model=list[NamedConfigResponse])
async def get_categories(
    kind: str | None = Query(default=None, pattern="^(income|expense)$"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await ensure_default_financial_categories(db, professional.id)
    return await service.list_named_configs(db, professional.id, FinancialCategory, kind=kind)


@router.post("/categories", response_model=NamedConfigResponse, status_code=status.HTTP_201_CREATED)
async def post_category(
    body: NamedConfigCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_named_config(
        db, professional.id, FinancialCategory, body, required_kind=True
    )


@router.patch("/categories/{category_id}", response_model=NamedConfigResponse)
async def patch_category(
    category_id: UUID,
    body: NamedConfigUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_named_config(db, professional.id, FinancialCategory, category_id, body)


@router.get("/payment-methods", response_model=list[NamedConfigResponse])
async def get_payment_methods(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await ensure_default_payment_methods(db, professional.id)
    return await service.list_named_configs(db, professional.id, PaymentMethod)


@router.post("/payment-methods", response_model=NamedConfigResponse, status_code=status.HTTP_201_CREATED)
async def post_payment_method(
    body: NamedConfigCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_named_config(db, professional.id, PaymentMethod, body)


@router.patch("/payment-methods/{method_id}", response_model=NamedConfigResponse)
async def patch_payment_method(
    method_id: UUID,
    body: NamedConfigUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_named_config(db, professional.id, PaymentMethod, method_id, body)


@router.get("/services", response_model=list[ServiceResponse])
async def get_services(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_services(db, professional.id)


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def post_service(
    body: ServiceCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_service(db, professional.id, body)


@router.patch("/services/{service_id}", response_model=ServiceResponse)
async def patch_service(
    service_id: UUID,
    body: ServiceUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_service(db, professional.id, service_id, body)


@router.get("/packages", response_model=list[PackageResponse])
async def get_packages(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_packages(db, professional.id)


@router.post("/packages", response_model=PackageResponse, status_code=status.HTTP_201_CREATED)
async def post_package(
    body: PackageCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_package(db, professional.id, body)


@router.patch("/packages/{package_id}", response_model=PackageResponse)
async def patch_package(
    package_id: UUID,
    body: PackageUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_package(db, professional.id, package_id, body)


@router.get("/receivables", response_model=ReceivableListResponse)
async def get_receivables(
    patient_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    q: str | None = Query(default=None, max_length=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    items = await service.list_receivables(
        db,
        professional.id,
        patient_id=patient_id,
        status_filter=status_filter,
        from_date=from_date,
        to_date=to_date,
        q=q,
    )
    return ReceivableListResponse(items=items, total=len(items))


@router.post("/receivables", response_model=ReceivableResponse, status_code=status.HTTP_201_CREATED)
async def post_receivable(
    body: ReceivableCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    entity = await service.create_receivable_entity(db, professional.id, body)
    return await service.serialize_receivable(db, entity)


@router.get("/receivables/{receivable_id}", response_model=ReceivableResponse)
async def get_receivable(
    receivable_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_receivable(db, professional.id, receivable_id)


@router.post("/receivables/{receivable_id}/cancel", response_model=ReceivableResponse)
async def cancel_receivable(
    receivable_id: UUID,
    body: CancellationRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.cancel_receivable(db, professional.id, receivable_id, body.reason)


@router.get("/payments", response_model=list[PaymentResponse])
async def get_payments(
    patient_id: UUID | None = None,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_payments(db, professional.id, patient_id=patient_id)


@router.post("/payments", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
async def post_payment(
    body: PaymentCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_payment(db, professional.id, body)


@router.post("/payments/{payment_id}/reverse", response_model=PaymentResponse)
async def reverse_payment(
    payment_id: UUID,
    body: CancellationRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.reverse_payment(db, professional.id, payment_id, body.reason)


@router.get("/payments/{payment_id}/receipt")
async def get_payment_receipt(
    payment_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    payment = await service._owned(db, FinancialPayment, payment_id, professional.id, "Recebimento")
    profile_result = await db.execute(
        select(FinancialProfile).where(FinancialProfile.professional_id == professional.id)
    )
    content = build_internal_receipt(payment, profile_result.scalar_one_or_none())
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{payment.receipt_number}.pdf"'},
    )


@router.get("/payables", response_model=PayableListResponse)
async def get_payables(
    status_filter: str | None = Query(default=None, alias="status"),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    q: str | None = Query(default=None, max_length=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    items = await service.list_payables(
        db, professional.id, status_filter=status_filter, from_date=from_date, to_date=to_date, q=q
    )
    return PayableListResponse(items=items, total=len(items))


@router.post("/payables", response_model=PayableResponse, status_code=status.HTTP_201_CREATED)
async def post_payable(
    body: PayableCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_payable(db, professional.id, body)


@router.post(
    "/payables/{payable_id}/settlements",
    response_model=SettlementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_payable_settlement(
    payable_id: UUID,
    body: SettlementCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.settle_payable(db, professional.id, payable_id, body)


@router.post(
    "/payables/{payable_id}/settlements/{settlement_id}/reverse",
    response_model=SettlementResponse,
)
async def reverse_payable_settlement(
    payable_id: UUID,
    settlement_id: UUID,
    body: CancellationRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.reverse_payable_settlement(
        db, professional.id, payable_id, settlement_id, body.reason
    )


@router.post("/payables/{payable_id}/cancel", response_model=PayableResponse)
async def cancel_payable(
    payable_id: UUID,
    body: CancellationRequest,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.cancel_payable(db, professional.id, payable_id, body.reason)


@router.get("/patient-packages", response_model=list[PatientPackageResponse])
async def get_patient_packages(
    patient_id: UUID | None = None,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_patient_packages(db, professional.id, patient_id=patient_id)


@router.post(
    "/patient-packages",
    response_model=PatientPackageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_patient_package(
    body: PatientPackageCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_patient_package(db, professional.id, body)


@router.get("/cash-flow", response_model=CashFlowResponse)
async def get_cash_flow(
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.cash_flow(db, professional.id, from_date, to_date)


@router.get("/dashboard", response_model=FinanceDashboardResponse)
async def get_dashboard(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.dashboard(db, professional.id)


@patient_router.get("", response_model=PatientFinanceResponse)
async def get_patient_finance(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    return await service.patient_finance(db, professional.id, patient_id)
