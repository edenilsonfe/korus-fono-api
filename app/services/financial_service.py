"""Business rules for the professional's internal clinic finance ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import (
    FinancialAuditEvent,
    FinancialCategory,
    FinancialPayment,
    FinancialProfile,
    PackageUsage,
    Payable,
    PayableSettlement,
    PatientPackage,
    PaymentAllocation,
    PaymentMethod,
    Receivable,
    ReceivableItem,
    ServiceOffering,
    ServicePackage,
)
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.finance import (
    CashFlowResponse,
    FinanceDashboardResponse,
    FinancialProfileResponse,
    FinancialProfileUpdate,
    NamedConfigCreate,
    NamedConfigResponse,
    PackageCreate,
    PackageResponse,
    PackageUpdate,
    PatientFinanceResponse,
    PatientPackageCreate,
    PatientPackageResponse,
    PayableCreate,
    PayableResponse,
    PaymentAllocationResponse,
    PaymentCreate,
    PaymentResponse,
    ReceivableCreate,
    ReceivableItemCreate,
    ReceivableItemResponse,
    ReceivableResponse,
    ServiceCreate,
    ServiceResponse,
    ServiceUpdate,
    SettlementCreate,
    SettlementResponse,
)


def _not_found(label: str = "Registro financeiro") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} não encontrado")


async def _audit(
    db: AsyncSession,
    professional_id: UUID,
    entity_type: str,
    entity_id: UUID,
    action: str,
    payload: dict | None = None,
) -> None:
    db.add(
        FinancialAuditEvent(
            professional_id=professional_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            payload=payload or {},
            created_at=datetime.now(UTC),
        )
    )


async def _patient(db: AsyncSession, professional_id: UUID, patient_id: UUID | None) -> Patient | None:
    if patient_id is None:
        return None
    result = await db.execute(
        select(Patient).where(Patient.id == patient_id, Patient.professional_id == professional_id)
    )
    patient = result.scalar_one_or_none()
    if not patient:
        raise _not_found("Paciente")
    return patient


async def _owned(
    db: AsyncSession,
    model,
    entity_id: UUID,
    professional_id: UUID,
    label: str,
    *,
    for_update: bool = False,
):
    query = select(model).where(model.id == entity_id, model.professional_id == professional_id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    entity = result.scalar_one_or_none()
    if not entity:
        raise _not_found(label)
    return entity


async def get_or_create_profile(
    db: AsyncSession, professional: Professional
) -> FinancialProfileResponse:
    result = await db.execute(
        select(FinancialProfile).where(FinancialProfile.professional_id == professional.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        profile = FinancialProfile(
            professional_id=professional.id,
            person_type="PF",
            legal_name=professional.name,
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)
    return FinancialProfileResponse.model_validate(profile)


async def update_profile(
    db: AsyncSession, professional: Professional, body: FinancialProfileUpdate
) -> FinancialProfileResponse:
    await get_or_create_profile(db, professional)
    result = await db.execute(
        select(FinancialProfile).where(FinancialProfile.professional_id == professional.id)
    )
    profile = result.scalar_one()
    for field, value in body.model_dump().items():
        setattr(profile, field, value.strip() if isinstance(value, str) else value)
    await _audit(db, professional.id, "financial_profile", profile.id, "updated")
    await db.commit()
    await db.refresh(profile)
    return FinancialProfileResponse.model_validate(profile)


async def list_named_configs(
    db: AsyncSession, professional_id: UUID, model, *, kind: str | None = None
) -> list[NamedConfigResponse]:
    query = select(model).where(model.professional_id == professional_id)
    if kind is not None:
        query = query.where(model.kind == kind)
    result = await db.execute(query.order_by(model.active.desc(), model.name.asc()))
    return [
        NamedConfigResponse(
            id=item.id,
            name=item.name,
            kind=getattr(item, "kind", None),
            active=item.active,
        )
        for item in result.scalars().all()
    ]


async def create_named_config(
    db: AsyncSession,
    professional_id: UUID,
    model,
    body: NamedConfigCreate,
    *,
    required_kind: bool = False,
) -> NamedConfigResponse:
    if required_kind and body.kind is None:
        raise HTTPException(status_code=422, detail="O tipo da categoria é obrigatório")
    values = {"professional_id": professional_id, "name": body.name}
    if required_kind:
        values["kind"] = body.kind
    entity = model(**values)
    db.add(entity)
    try:
        await db.flush()
        await _audit(db, professional_id, model.__tablename__, entity.id, "created")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Já existe uma configuração com este nome") from exc
    await db.refresh(entity)
    return NamedConfigResponse(
        id=entity.id,
        name=entity.name,
        kind=getattr(entity, "kind", None),
        active=entity.active,
    )


async def update_named_config(
    db: AsyncSession, professional_id: UUID, model, entity_id: UUID, body
) -> NamedConfigResponse:
    entity = await _owned(db, model, entity_id, professional_id, "Configuração")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(entity, field, value.strip() if isinstance(value, str) else value)
    await _audit(db, professional_id, model.__tablename__, entity.id, "updated")
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Já existe uma configuração com este nome") from exc
    await db.refresh(entity)
    return NamedConfigResponse(
        id=entity.id,
        name=entity.name,
        kind=getattr(entity, "kind", None),
        active=entity.active,
    )


async def _validate_category(
    db: AsyncSession, professional_id: UUID, category_id: UUID | None, kind: str
) -> None:
    if not category_id:
        return
    result = await db.execute(
        select(FinancialCategory.id).where(
            FinancialCategory.id == category_id,
            FinancialCategory.professional_id == professional_id,
            FinancialCategory.kind == kind,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Categoria inválida para este lançamento")


async def _validate_method(db: AsyncSession, professional_id: UUID, method_id: UUID | None) -> None:
    if not method_id:
        return
    result = await db.execute(
        select(PaymentMethod.id).where(
            PaymentMethod.id == method_id,
            PaymentMethod.professional_id == professional_id,
            PaymentMethod.active.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Forma de pagamento inválida")


async def create_service(
    db: AsyncSession, professional_id: UUID, body: ServiceCreate
) -> ServiceResponse:
    await _validate_category(db, professional_id, body.category_id, "income")
    entity = ServiceOffering(professional_id=professional_id, **body.model_dump())
    db.add(entity)
    await db.flush()
    await _audit(db, professional_id, "service", entity.id, "created")
    await db.commit()
    await db.refresh(entity)
    return ServiceResponse.model_validate(entity)


async def list_services(db: AsyncSession, professional_id: UUID) -> list[ServiceResponse]:
    result = await db.execute(
        select(ServiceOffering)
        .where(ServiceOffering.professional_id == professional_id)
        .order_by(ServiceOffering.active.desc(), ServiceOffering.name.asc())
    )
    return [ServiceResponse.model_validate(item) for item in result.scalars().all()]


async def update_service(
    db: AsyncSession, professional_id: UUID, service_id: UUID, body: ServiceUpdate
) -> ServiceResponse:
    entity = await _owned(db, ServiceOffering, service_id, professional_id, "Serviço")
    changes = body.model_dump(exclude_unset=True)
    if "category_id" in changes:
        await _validate_category(db, professional_id, changes["category_id"], "income")
    for field, value in changes.items():
        setattr(entity, field, value)
    await _audit(db, professional_id, "service", entity.id, "updated")
    await db.commit()
    await db.refresh(entity)
    return ServiceResponse.model_validate(entity)


async def create_package(
    db: AsyncSession, professional_id: UUID, body: PackageCreate
) -> PackageResponse:
    if body.service_id:
        await _owned(db, ServiceOffering, body.service_id, professional_id, "Serviço")
    entity = ServicePackage(professional_id=professional_id, **body.model_dump())
    db.add(entity)
    await db.flush()
    await _audit(db, professional_id, "package", entity.id, "created")
    await db.commit()
    await db.refresh(entity)
    return PackageResponse.model_validate(entity)


async def list_packages(db: AsyncSession, professional_id: UUID) -> list[PackageResponse]:
    result = await db.execute(
        select(ServicePackage)
        .where(ServicePackage.professional_id == professional_id)
        .order_by(ServicePackage.active.desc(), ServicePackage.name.asc())
    )
    return [PackageResponse.model_validate(item) for item in result.scalars().all()]


async def update_package(
    db: AsyncSession, professional_id: UUID, package_id: UUID, body: PackageUpdate
) -> PackageResponse:
    entity = await _owned(db, ServicePackage, package_id, professional_id, "Pacote")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("service_id"):
        await _owned(db, ServiceOffering, changes["service_id"], professional_id, "Serviço")
    for field, value in changes.items():
        setattr(entity, field, value)
    await _audit(db, professional_id, "package", entity.id, "updated")
    await db.commit()
    await db.refresh(entity)
    return PackageResponse.model_validate(entity)


async def create_receivable_entity(
    db: AsyncSession,
    professional_id: UUID,
    body: ReceivableCreate,
    *,
    appointment_id: UUID | None = None,
    commit: bool = True,
) -> Receivable:
    patient = await _patient(db, professional_id, body.patient_id)
    await _validate_category(db, professional_id, body.category_id, "income")
    total = sum(item.quantity * item.unit_cents for item in body.items)
    entity = Receivable(
        professional_id=professional_id,
        patient_id=body.patient_id,
        category_id=body.category_id,
        patient_name_snapshot=patient.name if patient else "",
        payer_name=body.payer_name.strip(),
        payer_document=body.payer_document.strip(),
        description=body.description.strip(),
        issue_date=body.issue_date,
        competence_date=body.competence_date or body.issue_date,
        due_date=body.due_date,
        total_cents=total,
        status="open",
        origin=body.origin,
        notes=body.notes,
    )
    db.add(entity)
    await db.flush()
    for index, item in enumerate(body.items):
        if item.service_id:
            await _owned(db, ServiceOffering, item.service_id, professional_id, "Serviço")
        db.add(
            ReceivableItem(
                receivable_id=entity.id,
                service_id=item.service_id,
                appointment_id=appointment_id if index == 0 else None,
                item_type=item.item_type,
                description=item.description.strip(),
                quantity=item.quantity,
                unit_cents=item.unit_cents,
                total_cents=item.quantity * item.unit_cents,
            )
        )
    await _audit(db, professional_id, "receivable", entity.id, "created", {"totalCents": total})
    if commit:
        await db.commit()
        await db.refresh(entity)
    return entity


async def _receivable_paid(db: AsyncSession, receivable_id: UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(PaymentAllocation.amount_cents), 0))
        .join(FinancialPayment, PaymentAllocation.payment_id == FinancialPayment.id)
        .where(
            PaymentAllocation.receivable_id == receivable_id,
            FinancialPayment.status == "confirmed",
        )
    )
    return int(result.scalar_one())


async def serialize_receivable(
    db: AsyncSession, entity: Receivable, *, include_items: bool = True
) -> ReceivableResponse:
    paid = await _receivable_paid(db, entity.id)
    balance = max(entity.total_cents - paid, 0)
    items: list[ReceivableItemResponse] = []
    if include_items:
        result = await db.execute(
            select(ReceivableItem)
            .where(ReceivableItem.receivable_id == entity.id)
            .order_by(ReceivableItem.created_at.asc())
        )
        items = [ReceivableItemResponse.model_validate(item) for item in result.scalars().all()]
    return ReceivableResponse(
        id=entity.id,
        patient_id=entity.patient_id,
        patient_name=entity.patient_name_snapshot,
        payer_name=entity.payer_name,
        payer_document=entity.payer_document,
        description=entity.description,
        issue_date=entity.issue_date,
        competence_date=entity.competence_date,
        due_date=entity.due_date,
        category_id=entity.category_id,
        total_cents=entity.total_cents,
        paid_cents=paid,
        balance_cents=balance,
        status=entity.status,
        overdue=entity.status not in {"paid", "canceled"} and balance > 0 and entity.due_date < date.today(),
        origin=entity.origin,
        notes=entity.notes,
        items=items,
    )


async def list_receivables(
    db: AsyncSession,
    professional_id: UUID,
    *,
    patient_id: UUID | None = None,
    status_filter: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    q: str | None = None,
) -> list[ReceivableResponse]:
    query = select(Receivable).where(Receivable.professional_id == professional_id)
    if patient_id:
        query = query.where(Receivable.patient_id == patient_id)
    if status_filter:
        if status_filter == "overdue":
            query = query.where(
                Receivable.status.in_(["open", "partially_paid"]), Receivable.due_date < date.today()
            )
        else:
            query = query.where(Receivable.status == status_filter)
    if from_date:
        query = query.where(Receivable.due_date >= from_date)
    if to_date:
        query = query.where(Receivable.due_date <= to_date)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Receivable.description.ilike(pattern),
                Receivable.payer_name.ilike(pattern),
                Receivable.patient_name_snapshot.ilike(pattern),
            )
        )
    result = await db.execute(query.order_by(Receivable.due_date.desc(), Receivable.created_at.desc()))
    return [await serialize_receivable(db, item, include_items=False) for item in result.scalars().all()]


async def get_receivable(
    db: AsyncSession, professional_id: UUID, receivable_id: UUID
) -> ReceivableResponse:
    entity = await _owned(db, Receivable, receivable_id, professional_id, "Conta a receber")
    return await serialize_receivable(db, entity)


async def _refresh_receivable_status(db: AsyncSession, receivable: Receivable) -> None:
    if receivable.status == "canceled":
        return
    paid = await _receivable_paid(db, receivable.id)
    receivable.status = "paid" if paid >= receivable.total_cents else "partially_paid" if paid else "open"


async def cancel_receivable(
    db: AsyncSession, professional_id: UUID, receivable_id: UUID, reason: str
) -> ReceivableResponse:
    entity = await _owned(
        db, Receivable, receivable_id, professional_id, "Conta a receber", for_update=True
    )
    if await _receivable_paid(db, entity.id):
        raise HTTPException(status_code=409, detail="Estorne os recebimentos antes de cancelar a conta")
    if entity.status != "canceled":
        entity.status = "canceled"
        entity.canceled_at = datetime.now(UTC)
        entity.cancellation_reason = reason
        await _audit(db, professional_id, "receivable", entity.id, "canceled", {"reason": reason})
        await db.commit()
        await db.refresh(entity)
    return await serialize_receivable(db, entity)


async def create_payment(
    db: AsyncSession, professional_id: UUID, body: PaymentCreate
) -> PaymentResponse:
    if sum(item.amount_cents for item in body.allocations) != body.amount_cents:
        raise HTTPException(status_code=422, detail="A soma das alocações deve ser igual ao valor recebido")
    if len({item.receivable_id for item in body.allocations}) != len(body.allocations):
        raise HTTPException(status_code=422, detail="Uma conta não pode aparecer duas vezes na mesma baixa")
    patient = await _patient(db, professional_id, body.patient_id)
    await _validate_method(db, professional_id, body.method_id)
    receivables: list[Receivable] = []
    allocations_by_id = {item.receivable_id: item for item in body.allocations}
    for receivable_id in sorted(allocations_by_id, key=str):
        allocation = allocations_by_id[receivable_id]
        result = await db.execute(
            select(Receivable)
            .where(
                Receivable.id == allocation.receivable_id,
                Receivable.professional_id == professional_id,
            )
            .with_for_update()
        )
        receivable = result.scalar_one_or_none()
        if not receivable:
            raise _not_found("Conta a receber")
        if receivable.status == "canceled":
            raise HTTPException(status_code=409, detail="Não é possível baixar uma conta cancelada")
        balance = receivable.total_cents - await _receivable_paid(db, receivable.id)
        if allocation.amount_cents > balance:
            raise HTTPException(status_code=422, detail="O valor recebido excede o saldo da conta")
        receivables.append(receivable)
    receipt_number = f"RC-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:12].upper()}"
    entity = FinancialPayment(
        professional_id=professional_id,
        patient_id=body.patient_id,
        method_id=body.method_id,
        patient_name_snapshot=patient.name if patient else "",
        payer_name=body.payer_name.strip(),
        payer_document=body.payer_document.strip(),
        payment_date=body.payment_date,
        amount_cents=body.amount_cents,
        status="confirmed",
        receipt_number=receipt_number[:40],
        notes=body.notes,
    )
    db.add(entity)
    await db.flush()
    for item in body.allocations:
        db.add(
            PaymentAllocation(
                payment_id=entity.id,
                receivable_id=item.receivable_id,
                amount_cents=item.amount_cents,
            )
        )
    await db.flush()
    for receivable in receivables:
        await _refresh_receivable_status(db, receivable)
    await _audit(db, professional_id, "payment", entity.id, "confirmed", {"amountCents": body.amount_cents})
    await db.commit()
    await db.refresh(entity)
    return await serialize_payment(db, entity)


async def serialize_payment(db: AsyncSession, entity: FinancialPayment) -> PaymentResponse:
    result = await db.execute(
        select(PaymentAllocation).where(PaymentAllocation.payment_id == entity.id)
    )
    allocations = [
        PaymentAllocationResponse(receivable_id=item.receivable_id, amount_cents=item.amount_cents)
        for item in result.scalars().all()
    ]
    return PaymentResponse(
        id=entity.id,
        patient_id=entity.patient_id,
        patient_name=entity.patient_name_snapshot,
        payer_name=entity.payer_name,
        payer_document=entity.payer_document,
        payment_date=entity.payment_date,
        amount_cents=entity.amount_cents,
        method_id=entity.method_id,
        status=entity.status,
        receipt_number=entity.receipt_number,
        notes=entity.notes,
        allocations=allocations,
    )


async def list_payments(
    db: AsyncSession, professional_id: UUID, *, patient_id: UUID | None = None
) -> list[PaymentResponse]:
    query = select(FinancialPayment).where(FinancialPayment.professional_id == professional_id)
    if patient_id:
        allocated_payment_ids = (
            select(PaymentAllocation.payment_id)
            .join(Receivable, PaymentAllocation.receivable_id == Receivable.id)
            .where(
                Receivable.professional_id == professional_id,
                Receivable.patient_id == patient_id,
            )
        )
        query = query.where(
            or_(
                FinancialPayment.patient_id == patient_id,
                FinancialPayment.id.in_(allocated_payment_ids),
            )
        )
    result = await db.execute(query.order_by(FinancialPayment.payment_date.desc()))
    return [await serialize_payment(db, item) for item in result.scalars().all()]


async def reverse_payment(
    db: AsyncSession, professional_id: UUID, payment_id: UUID, reason: str
) -> PaymentResponse:
    entity = await _owned(
        db, FinancialPayment, payment_id, professional_id, "Recebimento", for_update=True
    )
    if entity.status != "reversed":
        receivable_result = await db.execute(
            select(Receivable)
            .join(PaymentAllocation, PaymentAllocation.receivable_id == Receivable.id)
            .where(
                PaymentAllocation.payment_id == entity.id,
                Receivable.professional_id == professional_id,
            )
            .order_by(Receivable.id)
            .with_for_update()
        )
        receivables = receivable_result.scalars().all()
        entity.status = "reversed"
        entity.reversed_at = datetime.now(UTC)
        entity.reversal_reason = reason
        await db.flush()
        for receivable in receivables:
            await _refresh_receivable_status(db, receivable)
        await _audit(db, professional_id, "payment", entity.id, "reversed", {"reason": reason})
        await db.commit()
        await db.refresh(entity)
    return await serialize_payment(db, entity)


async def create_payable(
    db: AsyncSession, professional_id: UUID, body: PayableCreate
) -> PayableResponse:
    await _validate_category(db, professional_id, body.category_id, "expense")
    entity = Payable(
        professional_id=professional_id,
        description=body.description.strip(),
        supplier_name=body.supplier_name.strip(),
        issue_date=body.issue_date,
        competence_date=body.competence_date or body.issue_date,
        due_date=body.due_date,
        total_cents=body.total_cents,
        category_id=body.category_id,
        recurrence=body.recurrence,
        notes=body.notes,
        status="open",
    )
    db.add(entity)
    await db.flush()
    await _audit(db, professional_id, "payable", entity.id, "created")
    await db.commit()
    await db.refresh(entity)
    return await serialize_payable(db, entity)


async def _payable_paid(db: AsyncSession, payable_id: UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(PayableSettlement.amount_cents), 0)).where(
            PayableSettlement.payable_id == payable_id,
            PayableSettlement.status == "confirmed",
        )
    )
    return int(result.scalar_one())


async def serialize_payable(db: AsyncSession, entity: Payable) -> PayableResponse:
    paid = await _payable_paid(db, entity.id)
    balance = max(entity.total_cents - paid, 0)
    settlement_result = await db.execute(
        select(PayableSettlement)
        .where(PayableSettlement.payable_id == entity.id)
        .order_by(PayableSettlement.payment_date.desc(), PayableSettlement.created_at.desc())
    )
    return PayableResponse(
        id=entity.id,
        description=entity.description,
        supplier_name=entity.supplier_name,
        issue_date=entity.issue_date,
        competence_date=entity.competence_date,
        due_date=entity.due_date,
        total_cents=entity.total_cents,
        paid_cents=paid,
        balance_cents=balance,
        category_id=entity.category_id,
        status=entity.status,
        overdue=entity.status not in {"paid", "canceled"} and balance > 0 and entity.due_date < date.today(),
        recurrence=entity.recurrence,
        notes=entity.notes,
        settlements=[
            SettlementResponse.model_validate(settlement)
            for settlement in settlement_result.scalars().all()
        ],
    )


async def list_payables(
    db: AsyncSession,
    professional_id: UUID,
    *,
    status_filter: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    q: str | None = None,
) -> list[PayableResponse]:
    query = select(Payable).where(Payable.professional_id == professional_id)
    if status_filter:
        if status_filter == "overdue":
            query = query.where(Payable.status.in_(["open", "partially_paid"]), Payable.due_date < date.today())
        else:
            query = query.where(Payable.status == status_filter)
    if from_date:
        query = query.where(Payable.due_date >= from_date)
    if to_date:
        query = query.where(Payable.due_date <= to_date)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(Payable.description.ilike(pattern), Payable.supplier_name.ilike(pattern)))
    result = await db.execute(query.order_by(Payable.due_date.desc(), Payable.created_at.desc()))
    return [await serialize_payable(db, item) for item in result.scalars().all()]


async def settle_payable(
    db: AsyncSession, professional_id: UUID, payable_id: UUID, body: SettlementCreate
) -> SettlementResponse:
    result = await db.execute(
        select(Payable)
        .where(Payable.id == payable_id, Payable.professional_id == professional_id)
        .with_for_update()
    )
    entity = result.scalar_one_or_none()
    if not entity:
        raise _not_found("Conta a pagar")
    if entity.status == "canceled":
        raise HTTPException(status_code=409, detail="Não é possível pagar uma conta cancelada")
    await _validate_method(db, professional_id, body.method_id)
    balance = entity.total_cents - await _payable_paid(db, entity.id)
    if body.amount_cents > balance:
        raise HTTPException(status_code=422, detail="O valor pago excede o saldo da conta")
    settlement = PayableSettlement(
        payable_id=entity.id,
        professional_id=professional_id,
        payment_date=body.payment_date,
        amount_cents=body.amount_cents,
        method_id=body.method_id,
        notes=body.notes,
        status="confirmed",
    )
    db.add(settlement)
    await db.flush()
    new_paid = await _payable_paid(db, entity.id)
    entity.status = "paid" if new_paid >= entity.total_cents else "partially_paid"
    await _audit(db, professional_id, "payable_settlement", settlement.id, "confirmed")
    await db.commit()
    await db.refresh(settlement)
    return SettlementResponse.model_validate(settlement)


async def cancel_payable(
    db: AsyncSession, professional_id: UUID, payable_id: UUID, reason: str
) -> PayableResponse:
    entity = await _owned(
        db, Payable, payable_id, professional_id, "Conta a pagar", for_update=True
    )
    if await _payable_paid(db, entity.id):
        raise HTTPException(status_code=409, detail="Estorne os pagamentos antes de cancelar a conta")
    entity.status = "canceled"
    entity.canceled_at = datetime.now(UTC)
    entity.cancellation_reason = reason
    await _audit(db, professional_id, "payable", entity.id, "canceled", {"reason": reason})
    await db.commit()
    await db.refresh(entity)
    return await serialize_payable(db, entity)


async def reverse_payable_settlement(
    db: AsyncSession,
    professional_id: UUID,
    payable_id: UUID,
    settlement_id: UUID,
    reason: str,
) -> SettlementResponse:
    payable = await _owned(
        db, Payable, payable_id, professional_id, "Conta a pagar", for_update=True
    )
    result = await db.execute(
        select(PayableSettlement).where(
            PayableSettlement.id == settlement_id,
            PayableSettlement.payable_id == payable.id,
            PayableSettlement.professional_id == professional_id,
        )
    )
    settlement = result.scalar_one_or_none()
    if not settlement:
        raise _not_found("Pagamento")
    if settlement.status != "reversed":
        settlement.status = "reversed"
        settlement.reversed_at = datetime.now(UTC)
        settlement.reversal_reason = reason
        await db.flush()
        paid = await _payable_paid(db, payable.id)
        payable.status = "paid" if paid >= payable.total_cents else "partially_paid" if paid else "open"
        await _audit(
            db,
            professional_id,
            "payable_settlement",
            settlement.id,
            "reversed",
            {"reason": reason},
        )
        await db.commit()
        await db.refresh(settlement)
    return SettlementResponse.model_validate(settlement)


async def create_patient_package(
    db: AsyncSession, professional_id: UUID, body: PatientPackageCreate
) -> PatientPackageResponse:
    patient = await _patient(db, professional_id, body.patient_id)
    package = await _owned(db, ServicePackage, body.package_id, professional_id, "Pacote")
    if not package.active:
        raise HTTPException(status_code=409, detail="O pacote está inativo")
    receivable_body = ReceivableCreate(
        patient_id=patient.id,
        payer_name=body.payer_name,
        payer_document=body.payer_document,
        description=package.name,
        issue_date=body.started_on,
        competence_date=body.started_on,
        due_date=body.due_date,
        origin="package",
        items=[
            ReceivableItemCreate(
                description=package.name,
                quantity=1,
                unit_cents=package.price_cents,
                service_id=package.service_id,
                item_type="package",
            )
        ],
    )
    receivable = await create_receivable_entity(
        db, professional_id, receivable_body, commit=False
    )
    entity = PatientPackage(
        professional_id=professional_id,
        patient_id=patient.id,
        package_id=package.id,
        receivable_id=receivable.id,
        patient_name_snapshot=patient.name,
        package_name_snapshot=package.name,
        started_on=body.started_on,
        expires_on=body.started_on + timedelta(days=package.validity_days),
        sessions_included=package.sessions_count,
        sessions_used=0,
        agreed_price_cents=package.price_cents,
        status="active",
    )
    db.add(entity)
    await db.flush()
    await _audit(db, professional_id, "patient_package", entity.id, "created")
    await db.commit()
    await db.refresh(entity)
    return serialize_patient_package(entity)


def serialize_patient_package(entity: PatientPackage) -> PatientPackageResponse:
    return PatientPackageResponse(
        id=entity.id,
        patient_id=entity.patient_id,
        patient_name=entity.patient_name_snapshot,
        package_id=entity.package_id,
        package_name=entity.package_name_snapshot,
        receivable_id=entity.receivable_id,
        started_on=entity.started_on,
        expires_on=entity.expires_on,
        sessions_included=entity.sessions_included,
        sessions_used=entity.sessions_used,
        sessions_remaining=max(entity.sessions_included - entity.sessions_used, 0),
        agreed_price_cents=entity.agreed_price_cents,
        status=entity.status,
    )


async def list_patient_packages(
    db: AsyncSession, professional_id: UUID, *, patient_id: UUID | None = None
) -> list[PatientPackageResponse]:
    query = select(PatientPackage).where(PatientPackage.professional_id == professional_id)
    if patient_id:
        query = query.where(PatientPackage.patient_id == patient_id)
    result = await db.execute(query.order_by(PatientPackage.started_on.desc()))
    return [serialize_patient_package(item) for item in result.scalars().all()]


async def patient_finance(
    db: AsyncSession, professional_id: UUID, patient_id: UUID
) -> PatientFinanceResponse:
    await _patient(db, professional_id, patient_id)
    receivables = await list_receivables(db, professional_id, patient_id=patient_id)
    payments = await list_payments(db, professional_id, patient_id=patient_id)
    packages = await list_patient_packages(db, professional_id, patient_id=patient_id)
    return PatientFinanceResponse(
        patient_id=patient_id,
        receivables=receivables,
        payments=payments,
        packages=packages,
        open_balance_cents=sum(item.balance_cents for item in receivables if item.status != "canceled"),
    )


async def cash_flow(
    db: AsyncSession, professional_id: UUID, from_date: date, to_date: date
) -> CashFlowResponse:
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="O período informado é inválido")
    income_result = await db.execute(
        select(func.coalesce(func.sum(FinancialPayment.amount_cents), 0)).where(
            FinancialPayment.professional_id == professional_id,
            FinancialPayment.status == "confirmed",
            FinancialPayment.payment_date >= from_date,
            FinancialPayment.payment_date <= to_date,
        )
    )
    expense_result = await db.execute(
        select(func.coalesce(func.sum(PayableSettlement.amount_cents), 0)).where(
            PayableSettlement.professional_id == professional_id,
            PayableSettlement.status == "confirmed",
            PayableSettlement.payment_date >= from_date,
            PayableSettlement.payment_date <= to_date,
        )
    )
    receivables = await list_receivables(
        db, professional_id, from_date=from_date, to_date=to_date
    )
    payables = await list_payables(db, professional_id, from_date=from_date, to_date=to_date)
    realized_income = int(income_result.scalar_one())
    realized_expense = int(expense_result.scalar_one())
    projected_income = sum(item.balance_cents for item in receivables if item.status != "canceled")
    projected_expense = sum(item.balance_cents for item in payables if item.status != "canceled")
    return CashFlowResponse(
        from_date=from_date,
        to_date=to_date,
        realized_income_cents=realized_income,
        realized_expense_cents=realized_expense,
        projected_income_cents=projected_income,
        projected_expense_cents=projected_expense,
        realized_balance_cents=realized_income - realized_expense,
        projected_balance_cents=projected_income - projected_expense,
    )


async def dashboard(db: AsyncSession, professional_id: UUID) -> FinanceDashboardResponse:
    today = date.today()
    start = today.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    receivables = await list_receivables(db, professional_id)
    payables = await list_payables(db, professional_id)
    flow = await cash_flow(db, professional_id, start, end)
    active_receivables = [item for item in receivables if item.status != "canceled"]
    return FinanceDashboardResponse(
        open_receivables_cents=sum(item.balance_cents for item in active_receivables),
        overdue_receivables_cents=sum(item.balance_cents for item in active_receivables if item.overdue),
        received_this_month_cents=flow.realized_income_cents,
        payable_this_month_cents=sum(
            item.balance_cents for item in payables if item.status != "canceled" and start <= item.due_date <= end
        ),
        paid_this_month_cents=flow.realized_expense_cents,
        net_cash_this_month_cents=flow.realized_balance_cents,
        overdue_count=sum(1 for item in active_receivables if item.overdue),
    )
