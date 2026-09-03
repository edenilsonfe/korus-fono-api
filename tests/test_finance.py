"""Finance module integration tests.

The finance ledger belongs to the logged-in professional and is deliberately
separate from SaaS subscription billing.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token, hash_password
from app.models.appointment import Appointment
from app.models.finance import ReceivableItem, ServiceOffering
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    """Bind entitlement checks to the isolated test database."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


def _auth(professional: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


async def _create_receivable(client, headers, patient_id, amount, *, due_date=None, description="Sessão"):
    response = await client.post(
        "/api/v1/finance/receivables",
        headers=headers,
        json={
            "patientId": str(patient_id),
            "description": description,
            "dueDate": str(due_date or date.today()),
            "payerName": "Responsável financeiro",
            "items": [{"description": description, "quantity": 1, "unitCents": amount}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_payment_methods_include_idempotent_clinic_defaults(api_client, auth_headers):
    expected = {"Cartão de crédito", "Cartão de débito", "Pix", "Dinheiro"}

    first = await api_client.get("/api/v1/finance/payment-methods", headers=auth_headers)
    second = await api_client.get("/api/v1/finance/payment-methods", headers=auth_headers)

    assert first.status_code == 200, first.text
    assert {item["name"] for item in first.json()} == expected
    assert [item["name"] for item in second.json()].count("Pix") == 1
    assert len(second.json()) == 4


@pytest.mark.asyncio
async def test_financial_categories_include_defaults_and_classify_new_services(
    api_client, auth_headers
):
    expected_income = {
        "Atendimentos",
        "Avaliações",
        "Pacotes",
        "Taxas de cancelamento",
        "Outras receitas",
    }
    expected_expense = {
        "Aluguel e estrutura",
        "Materiais e insumos",
        "Serviços de terceiros",
        "Impostos e taxas",
        "Outras despesas",
    }

    first_income = await api_client.get(
        "/api/v1/finance/categories?kind=income", headers=auth_headers
    )
    second_income = await api_client.get(
        "/api/v1/finance/categories?kind=income", headers=auth_headers
    )
    expenses = await api_client.get(
        "/api/v1/finance/categories?kind=expense", headers=auth_headers
    )

    assert first_income.status_code == 200, first_income.text
    assert {item["name"] for item in first_income.json()} == expected_income
    assert {item["name"] for item in expenses.json()} == expected_expense
    assert len(second_income.json()) == len(expected_income)

    assessment_category = next(
        item for item in first_income.json() if item["name"] == "Avaliações"
    )
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={
            "name": "Avaliação fonoaudiológica",
            "duration": 60,
            "priceCents": 20_000,
            "categoryId": assessment_category["id"],
        },
    )

    assert service.status_code == 201, service.text
    assert service.json()["categoryId"] == assessment_category["id"]


@pytest.mark.asyncio
async def test_scheduling_a_service_snapshots_its_name_duration_and_price(
    api_client, db_session, patient, auth_headers
):
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Terapia de linguagem", "duration": 45, "priceCents": 18_500},
    )
    assert service.status_code == 201, service.text

    appointment_date = date.today() + timedelta(days=1)
    created = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": str(appointment_date),
            "time": "09:00",
            "type": "Valor legado que deve ser substituído",
            "duration": 30,
            "status": "confirmado",
            "serviceId": service.json()["id"],
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["serviceId"] == service.json()["id"]
    assert body["serviceName"] == "Terapia de linguagem"
    assert body["servicePriceCents"] == 18_500
    assert body["type"] == "Terapia de linguagem"
    assert body["duration"] == 45

    stored = await db_session.get(Appointment, UUID(body["id"]))
    assert stored is not None
    assert stored.service_id == UUID(service.json()["id"])
    assert stored.service_name_snapshot == "Terapia de linguagem"
    assert stored.service_price_cents == 18_500

    # Agendar registra o valor acordado, mas a dívida só nasce na conclusão.
    receivables = await api_client.get("/api/v1/finance/receivables", headers=auth_headers)
    assert receivables.status_code == 200
    assert receivables.json()["items"] == []


@pytest.mark.asyncio
async def test_recurring_series_children_keep_the_service_price_snapshot(
    api_client, db_session, patient, auth_headers
):
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Terapia de linguagem", "duration": 50, "priceCents": 17_000},
    )
    assert service.status_code == 201, service.text

    created = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": "2026-09-07",
            "time": "10:00",
            "type": "Terapia de linguagem",
            "duration": 50,
            "status": "confirmado",
            "serviceId": service.json()["id"],
            "appointmentType": "recorrente",
            "frequency": "semanal",
            "endDate": "2026-09-21",
        },
    )

    assert created.status_code == 201, created.text
    result = await db_session.execute(
        select(Appointment).where(Appointment.patient_id == patient.id)
    )
    appointments = result.scalars().all()
    assert len(appointments) >= 2
    service_id = UUID(service.json()["id"])
    assert all(item.service_id == service_id for item in appointments)
    assert all(item.service_name_snapshot == "Terapia de linguagem" for item in appointments)
    assert all(item.service_price_cents == 17_000 for item in appointments)


@pytest.mark.asyncio
async def test_appointment_rejects_a_financial_service_from_another_professional(
    api_client, db_session, professional, patient, auth_headers
):
    other = Professional(
        email="other-finance@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11977776666",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.flush()
    foreign_service = ServiceOffering(
        professional_id=other.id,
        name="Serviço de outra conta",
        duration=50,
        price_cents=10_000,
    )
    db_session.add(foreign_service)
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": str(date.today() + timedelta(days=3)),
            "time": "11:00",
            "type": "Serviço de outra conta",
            "duration": 50,
            "serviceId": str(foreign_service.id),
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Serviço financeiro não encontrado"


@pytest.mark.asyncio
async def test_updating_the_appointment_service_refreshes_its_snapshot(
    api_client, patient, auth_headers
):
    first = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Terapia", "duration": 50, "priceCents": 15_000},
    )
    second = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Avaliação", "duration": 60, "priceCents": 22_000},
    )
    created = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": str(date.today() + timedelta(days=4)),
            "time": "13:00",
            "type": "Terapia",
            "duration": 50,
            "serviceId": first.json()["id"],
        },
    )

    updated = await api_client.patch(
        f"/api/v1/appointments/{created.json()['id']}",
        headers=auth_headers,
        json={"serviceId": second.json()["id"]},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["serviceId"] == second.json()["id"]
    assert updated.json()["serviceName"] == "Avaliação"
    assert updated.json()["servicePriceCents"] == 22_000
    assert updated.json()["type"] == "Avaliação"
    assert updated.json()["duration"] == 60


@pytest.mark.asyncio
async def test_completion_uses_the_scheduled_price_snapshot_after_service_repricing(
    api_client, patient, auth_headers
):
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Avaliação inicial", "duration": 60, "priceCents": 20_000},
    )
    assert service.status_code == 201, service.text

    appointment_date = date.today() + timedelta(days=2)
    created = await api_client.post(
        "/api/v1/appointments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "date": str(appointment_date),
            "time": "10:00",
            "type": "Avaliação inicial",
            "duration": 60,
            "status": "confirmado",
            "serviceId": service.json()["id"],
        },
    )
    assert created.status_code == 201, created.text

    repriced = await api_client.patch(
        f"/api/v1/finance/services/{service.json()['id']}",
        headers=auth_headers,
        json={"name": "Avaliação inicial reajustada", "priceCents": 25_000},
    )
    assert repriced.status_code == 200, repriced.text

    completed = await api_client.post(
        f"/api/v1/appointments/{created.json()['id']}/complete",
        headers=auth_headers,
        json={
            "billingMode": "individual",
            "dueDate": str(appointment_date),
            "payerName": "Responsável financeiro",
        },
    )

    assert completed.status_code == 200, completed.text
    receivables = await api_client.get("/api/v1/finance/receivables", headers=auth_headers)
    assert receivables.status_code == 200
    receivable = receivables.json()["items"][0]
    assert receivable["description"] == "Avaliação inicial"
    assert receivable["totalCents"] == 20_000
    detail = await api_client.get(
        f"/api/v1/finance/receivables/{receivable['id']}", headers=auth_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["unitCents"] == 20_000


@pytest.mark.asyncio
async def test_receivables_support_partial_and_grouped_payments(
    api_client, professional, patient, auth_headers
):
    first = await _create_receivable(api_client, auth_headers, patient.id, 10_000)
    second = await _create_receivable(api_client, auth_headers, patient.id, 5_000, description="Avaliação")

    paid = await api_client.post(
        "/api/v1/finance/payments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "payerName": "Responsável financeiro",
            "paymentDate": str(date.today()),
            "amountCents": 12_000,
            "allocations": [
                {"receivableId": first["id"], "amountCents": 7_000},
                {"receivableId": second["id"], "amountCents": 5_000},
            ],
        },
    )

    assert paid.status_code == 201, paid.text
    listed = await api_client.get("/api/v1/finance/receivables", headers=auth_headers)
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[first["id"]]["status"] == "partially_paid"
    assert by_id[first["id"]]["balanceCents"] == 3_000
    assert by_id[second["id"]]["status"] == "paid"
    assert by_id[second["id"]]["balanceCents"] == 0


@pytest.mark.asyncio
async def test_grouped_payment_is_visible_by_each_patient_allocation(
    api_client, db_session, professional, patient, auth_headers
):
    sibling = Patient(
        professional_id=professional.id,
        name="Maria Silva",
        birth_date=date.today().replace(year=date.today().year - 6),
        diagnosis_keys=[],
        status="ativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(sibling)
    first = await _create_receivable(api_client, auth_headers, patient.id, 7_000)
    second = await _create_receivable(api_client, auth_headers, sibling.id, 3_000)

    payment = await api_client.post(
        "/api/v1/finance/payments",
        headers=auth_headers,
        json={
            "payerName": "Responsável dos irmãos",
            "paymentDate": str(date.today()),
            "amountCents": 10_000,
            "allocations": [
                {"receivableId": first["id"], "amountCents": 7_000},
                {"receivableId": second["id"], "amountCents": 3_000},
            ],
        },
    )
    assert payment.status_code == 201, payment.text

    first_history = await api_client.get(
        f"/api/v1/patients/{patient.id}/finance", headers=auth_headers
    )
    second_history = await api_client.get(
        f"/api/v1/patients/{sibling.id}/finance", headers=auth_headers
    )
    assert first_history.json()["payments"][0]["id"] == payment.json()["id"]
    assert second_history.json()["payments"][0]["id"] == payment.json()["id"]


@pytest.mark.asyncio
async def test_finance_is_scoped_to_professional(api_client, db_session, professional, patient, auth_headers):
    mine = await _create_receivable(api_client, auth_headers, patient.id, 8_000)
    other = Professional(
        email="finance-other@example.com",
        password_hash=hash_password("testpass123"),
        name="Outra profissional",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other)
    await db_session.commit()

    missing = await api_client.get(
        f"/api/v1/finance/receivables/{mine['id']}", headers=_auth(other)
    )
    assert missing.status_code == 404
    other_list = await api_client.get("/api/v1/finance/receivables", headers=_auth(other))
    assert other_list.status_code == 200
    assert other_list.json()["items"] == []


@pytest.mark.asyncio
async def test_payables_and_cash_flow_separate_projected_from_realized(
    api_client, professional, auth_headers
):
    future = date.today() + timedelta(days=10)
    created = await api_client.post(
        "/api/v1/finance/payables",
        headers=auth_headers,
        json={
            "description": "Aluguel futuro",
            "supplierName": "Locador",
            "dueDate": str(future),
            "totalCents": 20_000,
        },
    )
    assert created.status_code == 201, created.text
    payable = created.json()

    settled = await api_client.post(
        f"/api/v1/finance/payables/{payable['id']}/settlements",
        headers=auth_headers,
        json={"paymentDate": str(date.today()), "amountCents": 5_000},
    )
    assert settled.status_code == 201, settled.text

    flow = await api_client.get(
        f"/api/v1/finance/cash-flow?from={date.today()}&to={future}", headers=auth_headers
    )
    assert flow.status_code == 200, flow.text
    data = flow.json()
    assert data["realizedExpenseCents"] == 5_000
    assert data["projectedExpenseCents"] == 15_000


@pytest.mark.asyncio
async def test_complete_appointment_creates_one_session_and_one_receivable(
    api_client, db_session, professional, patient, auth_headers
):
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Terapia individual", "duration": 50, "priceCents": 15_000},
    )
    assert service.status_code == 201, service.text

    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    payload = {
        "billingMode": "individual",
        "serviceId": service.json()["id"],
        "dueDate": str(date.today()),
        "payerName": "Responsável financeiro",
    }
    first = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete", headers=auth_headers, json=payload
    )
    second = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete", headers=auth_headers, json=payload
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["sessionId"] == second.json()["sessionId"]
    assert first.json()["receivableId"] == second.json()["receivableId"]
    listed = await api_client.get("/api/v1/finance/receivables", headers=auth_headers)
    assert len(listed.json()["items"]) == 1


@pytest.mark.asyncio
async def test_complete_appointment_reuses_linked_evolution_session_and_still_bills(
    api_client, db_session, professional, patient, auth_headers
):
    service = await api_client.post(
        "/api/v1/finance/services",
        headers=auth_headers,
        json={"name": "Terapia individual", "duration": 50, "priceCents": 15_000},
    )
    assert service.status_code == 201, service.text

    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    linked_session = Session(
        professional_id=professional.id,
        patient_id=patient.id,
        appointment_id=appointment.id,
        date=datetime.combine(appointment.date, appointment.time),
        type=appointment.type,
        duration=appointment.duration,
        objectives=literal_column("'[]'"),
        notes="Evolução registrada antes da conclusão",
    )
    db_session.add(linked_session)
    await db_session.commit()
    await db_session.refresh(linked_session)

    completed = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={
            "billingMode": "individual",
            "serviceId": service.json()["id"],
            "dueDate": str(date.today()),
            "payerName": "Responsável financeiro",
        },
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["sessionId"] == str(linked_session.id)
    assert completed.json()["receivableId"] is not None
    await db_session.refresh(appointment)
    assert appointment.status == "concluido"
    sessions = await db_session.scalars(
        select(Session).where(Session.appointment_id == appointment.id)
    )
    assert len(sessions.all()) == 1


@pytest.mark.asyncio
async def test_session_created_from_appointment_is_explicitly_linked(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=auth_headers,
        json={
            "appointmentId": str(appointment.id),
            "date": f"{appointment.date}T{appointment.time.strftime('%H:%M')}:00",
            "type": appointment.type,
            "duration": appointment.duration,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["appointmentId"] == str(appointment.id)
    created = await db_session.get(Session, UUID(response.json()["id"]))
    assert created is not None
    assert created.appointment_id == appointment.id


@pytest.mark.asyncio
async def test_session_cannot_link_an_appointment_from_another_patient(
    api_client, db_session, professional, patient, auth_headers
):
    sibling = Patient(
        professional_id=professional.id,
        name="Outro paciente",
        birth_date=date.today().replace(year=date.today().year - 7),
        diagnosis_keys=[],
        status="ativo",
        start_date=date.today(),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(sibling)
    await db_session.flush()
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=sibling.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()

    response = await api_client.post(
        f"/api/v1/patients/{patient.id}/sessions",
        headers=auth_headers,
        json={
            "appointmentId": str(appointment.id),
            "date": f"{appointment.date}T{appointment.time.strftime('%H:%M')}:00",
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_updating_package_price_preserves_existing_financial_records(
    api_client, db_session, patient, auth_headers
):
    package = await api_client.post(
        "/api/v1/finance/packages",
        headers=auth_headers,
        json={"name": "Pacote mensal", "sessionsCount": 4, "priceCents": 40_000, "validityDays": 35},
    )
    assert package.status_code == 201, package.text
    enrollment = await api_client.post(
        "/api/v1/finance/patient-packages",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "packageId": package.json()["id"],
            "startedOn": str(date.today()),
            "dueDate": str(date.today()),
            "payerName": "Responsável financeiro",
        },
    )
    assert enrollment.status_code == 201, enrollment.text

    updated = await api_client.patch(
        f"/api/v1/finance/packages/{package.json()['id']}",
        headers=auth_headers,
        json={"priceCents": 50_000},
    )
    patient_finance = await api_client.get(
        f"/api/v1/patients/{patient.id}/finance", headers=auth_headers
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["priceCents"] == 50_000
    assert patient_finance.status_code == 200, patient_finance.text
    registered_package = next(
        item for item in patient_finance.json()["packages"] if item["id"] == enrollment.json()["id"]
    )
    registered_receivable = next(
        item
        for item in patient_finance.json()["receivables"]
        if item["id"] == enrollment.json()["receivableId"]
    )
    assert registered_package["agreedPriceCents"] == 40_000
    assert registered_receivable["totalCents"] == 40_000
    registered_item = await db_session.scalar(
        select(ReceivableItem).where(
            ReceivableItem.receivable_id == UUID(enrollment.json()["receivableId"])
        )
    )
    assert registered_item is not None
    assert registered_item.unit_cents == 40_000


@pytest.mark.asyncio
async def test_package_usage_prevents_an_individual_charge(
    api_client, db_session, professional, patient, auth_headers
):
    package = await api_client.post(
        "/api/v1/finance/packages",
        headers=auth_headers,
        json={"name": "Pacote mensal", "sessionsCount": 4, "priceCents": 40_000, "validityDays": 35},
    )
    assert package.status_code == 201, package.text
    enrollment = await api_client.post(
        "/api/v1/finance/patient-packages",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "packageId": package.json()["id"],
            "startedOn": str(date.today()),
            "dueDate": str(date.today()),
            "payerName": "Responsável financeiro",
        },
    )
    assert enrollment.status_code == 201, enrollment.text

    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Terapia individual",
        duration=50,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    completed = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={"billingMode": "package", "patientPackageId": enrollment.json()["id"]},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["receivableId"] is None
    assert completed.json()["packageUsageId"] is not None

    patient_finance = await api_client.get(
        f"/api/v1/patients/{patient.id}/finance", headers=auth_headers
    )
    assert patient_finance.status_code == 200
    assert patient_finance.json()["packages"][0]["sessionsUsed"] == 1


@pytest.mark.asyncio
async def test_payment_receipt_is_internal_pdf(api_client, patient, auth_headers):
    receivable = await _create_receivable(api_client, auth_headers, patient.id, 9_000)
    payment = await api_client.post(
        "/api/v1/finance/payments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "payerName": "Responsável financeiro",
            "paymentDate": str(date.today()),
            "amountCents": 9_000,
            "allocations": [{"receivableId": receivable["id"], "amountCents": 9_000}],
        },
    )
    assert payment.status_code == 201, payment.text

    receipt = await api_client.get(
        f"/api/v1/finance/payments/{payment.json()['id']}/receipt", headers=auth_headers
    )
    assert receipt.status_code == 200
    assert receipt.headers["content-type"] == "application/pdf"
    assert receipt.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_payment_cannot_exceed_balance_and_can_be_reversed(
    api_client, patient, auth_headers
):
    receivable = await _create_receivable(api_client, auth_headers, patient.id, 10_000)
    excessive = await api_client.post(
        "/api/v1/finance/payments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "payerName": "Responsável financeiro",
            "paymentDate": str(date.today()),
            "amountCents": 10_001,
            "allocations": [{"receivableId": receivable["id"], "amountCents": 10_001}],
        },
    )
    assert excessive.status_code == 422

    payment = await api_client.post(
        "/api/v1/finance/payments",
        headers=auth_headers,
        json={
            "patientId": str(patient.id),
            "payerName": "Responsável financeiro",
            "paymentDate": str(date.today()),
            "amountCents": 4_000,
            "allocations": [{"receivableId": receivable["id"], "amountCents": 4_000}],
        },
    )
    assert payment.status_code == 201
    blocked_cancel = await api_client.post(
        f"/api/v1/finance/receivables/{receivable['id']}/cancel",
        headers=auth_headers,
        json={"reason": "Lançamento duplicado"},
    )
    assert blocked_cancel.status_code == 409

    reversed_payment = await api_client.post(
        f"/api/v1/finance/payments/{payment.json()['id']}/reverse",
        headers=auth_headers,
        json={"reason": "Baixa feita por engano"},
    )
    assert reversed_payment.status_code == 200
    assert reversed_payment.json()["status"] == "reversed"
    canceled = await api_client.post(
        f"/api/v1/finance/receivables/{receivable['id']}/cancel",
        headers=auth_headers,
        json={"reason": "Lançamento duplicado"},
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_courtesy_completion_creates_session_without_charge(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Orientação",
        duration=30,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    completed = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={"billingMode": "courtesy", "notes": "Cortesia registrada explicitamente"},
    )
    assert completed.status_code == 200
    assert completed.json()["receivableId"] is None
    assert completed.json()["packageUsageId"] is None
    listed = await api_client.get("/api/v1/finance/receivables", headers=auth_headers)
    assert listed.json()["items"] == []


@pytest.mark.asyncio
async def test_repeated_courtesy_completion_reports_original_mode(
    api_client, db_session, professional, patient, auth_headers
):
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=date.today(),
        time=datetime.now().time().replace(second=0, microsecond=0),
        type="Orientação",
        duration=30,
        status="confirmado",
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)

    first = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={"billingMode": "courtesy"},
    )
    repeated = await api_client.post(
        f"/api/v1/appointments/{appointment.id}/complete",
        headers=auth_headers,
        json={"billingMode": "individual"},
    )

    assert first.status_code == 200, first.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["billingMode"] == "courtesy"
    assert repeated.json()["sessionId"] == first.json()["sessionId"]
    assert repeated.json()["receivableId"] is None


@pytest.mark.asyncio
async def test_payable_settlement_reversal_reopens_balance(api_client, auth_headers):
    payable = await api_client.post(
        "/api/v1/finance/payables",
        headers=auth_headers,
        json={
            "description": "Supervisão clínica",
            "dueDate": str(date.today()),
            "totalCents": 12_000,
        },
    )
    settlement = await api_client.post(
        f"/api/v1/finance/payables/{payable.json()['id']}/settlements",
        headers=auth_headers,
        json={"paymentDate": str(date.today()), "amountCents": 12_000},
    )
    reversed_settlement = await api_client.post(
        f"/api/v1/finance/payables/{payable.json()['id']}/settlements/{settlement.json()['id']}/reverse",
        headers=auth_headers,
        json={"reason": "Pagamento devolvido"},
    )
    assert reversed_settlement.status_code == 200
    assert reversed_settlement.json()["status"] == "reversed"
    payables = await api_client.get("/api/v1/finance/payables", headers=auth_headers)
    assert payables.json()["items"][0]["status"] == "open"
    assert payables.json()["items"][0]["balanceCents"] == 12_000
    assert payables.json()["items"][0]["settlements"][0]["status"] == "reversed"
