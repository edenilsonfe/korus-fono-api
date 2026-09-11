"""F20 fase 2 — public acknowledgement (receipt) of school report deliveries.

Behaviour under test (plan §3.2, task 2.2):

- ``POST /api/v1/report-deliveries/{token}/acknowledgement`` is idempotent: the
  first confirmation writes the self-declared identity plus the server
  timestamp; replays return the first receipt and never replace that identity.
- Read paths (GET/HEAD/export) never confirm; only the explicit POST does.
- Validity mirrors the public GET: unknown/expired/revoked token or deactivated
  account -> 410; non-school delivery -> 409; invalid payload -> 422.
- The public rate limit is mocked here (its own suite covers the budgets): a
  denied counter is 429 and an unreachable counter store is 503 (fail-closed).
- Entitlement: a stale professional cookie in read-only mode cannot block the
  school confirmation, but the token must still be valid and no other mutation
  path is released.
"""

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.auth_cookies import ACCESS_COOKIE
from app.core.security import create_access_token
from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_delivery import ReportDelivery
from app.schemas.report_delivery import ReportReceiptCreate
from app.services import report_delivery_service, school_report_delivery_service
from app.services.report_delivery_service import InvalidReportDeliveryToken

SCHOOL_NAME = "Escola Municipal Vila Nova"
SCHOOL_RECIPIENT = "Coordenação Ana"
EVIDENCE_TEXT = "Termo de autorização assinado arquivado no prontuário"
RECEIVER_NAME = "Ana Coordenadora"
RECEIVER_ROLE = "Coordenação pedagógica"

RESTRICTED_PUBLIC_KEYS = (
    "schoolAuthorization",
    "authorizationRecordedAt",
    "schoolName",
    "schoolRecipientName",
    "email",
    "caregiverId",
    "receivedByName",
    "receivedByRole",
)


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Receipt tests must not depend on a local Redis; budgets are unit-tested."""

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


@pytest.fixture
async def escolar_report(db_session, professional, patient):
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="escolar",
        date=date(2026, 9, 1),
        preview="Relatório escolar revisado",
        content="## Síntese\nTexto escolar revisado pela profissional.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


@pytest.fixture
async def caregiver(db_session, patient):
    return await db_session.scalar(
        select(Caregiver).where(Caregiver.patient_id == patient.id)
    )


def _school_authorization(caregiver_id) -> dict:
    return {
        "caregiverId": str(caregiver_id),
        "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "evidenceReference": EVIDENCE_TEXT,
        "reviewed": True,
    }


async def _create_school_delivery(api_client, auth_headers, report, caregiver):
    response = await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json={
            "channel": "link",
            "recipientKind": "school",
            "school": {"name": SCHOOL_NAME, "recipientName": SCHOOL_RECIPIENT},
            "schoolAuthorization": _school_authorization(caregiver.id),
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    token = data["url"].rsplit("/", 1)[-1]
    return data, token


def _receipt_payload(**overrides) -> dict:
    payload = {
        "received": True,
        "receiverName": RECEIVER_NAME,
        "receiverRole": RECEIVER_ROLE,
    }
    payload.update(overrides)
    return payload


async def _ack(api_client, token, **overrides):
    return await api_client.post(
        f"/api/v1/report-deliveries/{token}/acknowledgement",
        json=_receipt_payload(**overrides),
    )


async def _stored_delivery(db_session, delivery_id) -> ReportDelivery:
    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(str(delivery_id)))
    )
    await db_session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# First confirmation, public projection and replay
# --------------------------------------------------------------------------- #


async def test_first_acknowledgement_records_server_receipt(
    api_client, auth_headers, db_session, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    public = (await api_client.get(f"/api/v1/report-deliveries/{token}")).json()
    assert public["recipientKind"] == "school"
    assert public["requiresAcknowledgement"] is True
    assert public["receivedAt"] is None
    assert public["reportVersion"] == created["reportVersion"] == 1
    assert public["contentHash"] == created["contentHash"]
    for restricted in RESTRICTED_PUBLIC_KEYS:
        assert restricted not in public

    before = datetime.now(UTC) - timedelta(seconds=5)
    ack = await _ack(api_client, token, receiverName=f"  {RECEIVER_NAME}  ")
    assert ack.status_code == 200, ack.text
    receipt = ack.json()
    assert receipt["reportVersion"] == 1
    assert receipt["contentHash"] == created["contentHash"]
    assert receipt["receivedAt"]

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is not None
    received_at = (
        row.received_at
        if row.received_at.tzinfo
        else row.received_at.replace(tzinfo=UTC)
    )
    assert received_at >= before  # server timestamp, not client-provided
    assert row.received_by_name == RECEIVER_NAME  # stripped, self-declared
    assert row.received_by_role == RECEIVER_ROLE

    after = (await api_client.get(f"/api/v1/report-deliveries/{token}")).json()
    assert after["receivedAt"] is not None
    assert after["requiresAcknowledgement"] is True


async def test_repeated_acknowledgement_returns_first_receipt_and_identity(
    api_client, auth_headers, db_session, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    first = await _ack(api_client, token)
    assert first.status_code == 200
    again = await _ack(
        api_client, token, receiverName="Bruno Diretor", receiverRole="Direção"
    )
    assert again.status_code == 200

    assert again.json()["receivedAt"] == first.json()["receivedAt"]
    assert again.json()["reportVersion"] == first.json()["reportVersion"]
    assert again.json()["contentHash"] == first.json()["contentHash"]

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_by_name == RECEIVER_NAME
    assert row.received_by_role == RECEIVER_ROLE


async def test_read_paths_never_confirm(
    api_client, auth_headers, db_session, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    assert (
        await api_client.get(f"/api/v1/report-deliveries/{token}")
    ).status_code == 200
    # This FastAPI version does not route HEAD to GET handlers (405); either
    # way a read-only probe can never write the receipt.
    head = await api_client.head(f"/api/v1/report-deliveries/{token}")
    assert head.status_code in (200, 405)
    export = await api_client.get(
        f"/api/v1/report-deliveries/{token}/export", params={"format": "txt"}
    )
    assert export.status_code == 200

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is None
    public = (await api_client.get(f"/api/v1/report-deliveries/{token}")).json()
    assert public["receivedAt"] is None


# --------------------------------------------------------------------------- #
# Invalid link/state: 410 and 409
# --------------------------------------------------------------------------- #


async def test_acknowledgement_410_for_unknown_token(api_client):
    response = await _ack(api_client, "not-a-real-token")
    assert response.status_code == 410
    assert "inválido" in response.json()["detail"]


async def test_acknowledgement_410_after_revoke(
    api_client, auth_headers, db_session, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )
    revoke = await api_client.delete(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries/{created['id']}",
        headers=auth_headers,
    )
    assert revoke.status_code == 200

    response = await _ack(api_client, token)
    assert response.status_code == 410
    assert "revogado" in response.json()["detail"]

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is None


async def test_acknowledgement_410_for_expired_token(
    api_client, auth_headers, db_session, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )
    row = await _stored_delivery(db_session, created["id"])
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    response = await _ack(api_client, token)
    assert response.status_code == 410
    assert (await _stored_delivery(db_session, created["id"])).received_at is None


async def test_acknowledgement_410_when_account_deactivated(
    api_client, auth_headers, db_session, professional, escolar_report, caregiver
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )
    professional.is_disabled = True
    await db_session.commit()

    assert (
        await api_client.get(f"/api/v1/report-deliveries/{token}")
    ).status_code == 410
    response = await _ack(api_client, token)
    assert response.status_code == 410
    assert (await _stored_delivery(db_session, created["id"])).received_at is None


async def test_standard_delivery_returns_409_and_writes_nothing(
    api_client, auth_headers, db_session, escolar_report
):
    created = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert created.status_code == 201
    data = created.json()
    assert data["recipientKind"] == "standard"
    token = data["url"].rsplit("/", 1)[-1]

    public = (await api_client.get(f"/api/v1/report-deliveries/{token}")).json()
    assert public["requiresAcknowledgement"] is False

    response = await _ack(api_client, token)
    assert response.status_code == 409
    assert "confirmação" in response.json()["detail"]

    row = await _stored_delivery(db_session, data["id"])
    assert row.received_at is None
    assert row.received_by_name is None


@pytest.mark.parametrize(
    "payload",
    [
        {"received": False, "receiverName": RECEIVER_NAME, "receiverRole": RECEIVER_ROLE},
        {"received": True, "receiverName": "", "receiverRole": RECEIVER_ROLE},
        {"received": True, "receiverName": "   ", "receiverRole": RECEIVER_ROLE},
        {"received": True, "receiverName": "A" * 161, "receiverRole": RECEIVER_ROLE},
        {"received": True, "receiverName": RECEIVER_NAME, "receiverRole": "R" * 121},
        {"received": True, "receiverName": RECEIVER_NAME},
        {"receiverName": RECEIVER_NAME, "receiverRole": RECEIVER_ROLE},
        {
            "received": True,
            "receiverName": RECEIVER_NAME,
            "receiverRole": RECEIVER_ROLE,
            "receivedAt": "2020-01-01T00:00:00+00:00",
        },
        {
            "received": True,
            "receiverName": RECEIVER_NAME,
            "receiverRole": RECEIVER_ROLE,
            "extra": "nope",
        },
    ],
)
async def test_invalid_payload_returns_422(
    api_client, auth_headers, db_session, escolar_report, caregiver, payload
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    response = await api_client.post(
        f"/api/v1/report-deliveries/{token}/acknowledgement", json=payload
    )
    assert response.status_code == 422

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is None


# --------------------------------------------------------------------------- #
# Public rate limit wiring (budgets live in test_clinical_public_rate_limit)
# --------------------------------------------------------------------------- #


async def test_endpoint_limits_hashed_token_then_ip(
    api_client, auth_headers, escolar_report, caregiver, monkeypatch
):
    _, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    calls: list[tuple] = []

    def fake_allow(*, key, max_requests, window_seconds):
        calls.append((key, max_requests, window_seconds))
        return True

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", fake_allow
    )

    response = await _ack(api_client, token)
    assert response.status_code == 200

    assert len(calls) == 2
    assert calls[0][0].endswith(f":token:{hashlib.sha256(token.encode()).hexdigest()}")
    assert calls[0][1] == 10 and calls[0][2] == 60
    assert calls[1][0].startswith("clinical:report-delivery-ack:ip:")
    assert calls[1][1] == 60 and calls[1][2] == 60
    assert all(token not in call[0] for call in calls)


async def test_rate_limited_acknowledgement_returns_429(
    api_client, auth_headers, db_session, escolar_report, caregiver, monkeypatch
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: False
    )

    response = await _ack(api_client, token)
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "60"
    assert "Muitas confirmações" in response.json()["detail"]

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is None


async def test_acknowledgement_fails_closed_without_counter_store(
    api_client, auth_headers, db_session, escolar_report, caregiver, monkeypatch
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )

    def _store_down(**_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", _store_down
    )

    response = await _ack(api_client, token)
    assert response.status_code == 503
    assert "indisponível" in response.json()["detail"]

    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is None


# --------------------------------------------------------------------------- #
# Entitlement: stale professional cookie in read-only must not block the school
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("subscription_status", ["trial_expired", "past_due", "canceled"])
async def test_read_only_professional_cookie_cannot_block_acknowledgement(
    api_client,
    auth_headers,
    db_session,
    professional,
    escolar_report,
    caregiver,
    subscription_status,
):
    created, token = await _create_school_delivery(
        api_client, auth_headers, escolar_report, caregiver
    )
    professional.subscription_status = subscription_status
    await db_session.commit()
    cookie = {ACCESS_COOKIE: create_access_token(professional.id)}

    # Read-only still blocks the professional's own mutations...
    blocked = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert blocked.status_code == 403

    # ...but the school confirmation goes through with the same stale cookie.
    ack = await api_client.post(
        f"/api/v1/report-deliveries/{token}/acknowledgement",
        json=_receipt_payload(),
        cookies=cookie,
    )
    assert ack.status_code == 200, ack.text
    row = await _stored_delivery(db_session, created["id"])
    assert row.received_at is not None

    # The exemption is exact: no other mutation path is released.
    other = await api_client.post(
        f"/api/v1/report-deliveries/{token}", json={}, cookies=cookie
    )
    assert other.status_code == 403

    # Token validity is still mandatory with the cookie present.
    gone = await api_client.post(
        "/api/v1/report-deliveries/not-a-real-token/acknowledgement",
        json=_receipt_payload(),
        cookies=cookie,
    )
    assert gone.status_code == 410


# --------------------------------------------------------------------------- #
# Real concurrency (PostgreSQL gate): one receipt, first identity, no deadlock
# --------------------------------------------------------------------------- #


async def _seed_school_delivery(factory, *, token: str):
    async with factory() as db:
        pro = Professional(
            email=f"race-{uuid4().hex}@example.com",
            name="Race",
            password_hash="unused",
        )
        db.add(pro)
        await db.flush()
        patient = Patient(
            professional_id=pro.id,
            name="Synthetic",
            birth_date=date(2020, 1, 1),
            start_date=date.today(),
            avatar_color="teal",
            diagnosis_keys=[],
        )
        db.add(patient)
        await db.flush()
        report = AIReport(
            professional_id=pro.id,
            patient_id=patient.id,
            type="escolar",
            date=date(2026, 9, 1),
            preview="Escolar",
            content="## Síntese\nTexto.",
            status="finalized",
        )
        db.add(report)
        await db.flush()
        delivery = ReportDelivery(
            report_id=report.id,
            professional_id=pro.id,
            patient_id=patient.id,
            channel="link",
            recipient_kind="school",
            recipient_label="Escola · Ana",
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            school_name="Escola",
            school_recipient_name="Ana",
            school_authorization={"caregiverId": str(uuid4()), "reviewed": True},
            authorization_recorded_at=datetime.now(UTC),
            document_snapshot={
                "formatVersion": 1,
                "reportType": "escolar",
                "reportDate": "2026-09-01",
                "patientName": "Synthetic",
                "professionalName": "Race",
                "professionalCouncil": "",
                "reportVersion": 1,
                "contentHash": "b" * 64,
                "content": "## Síntese\nTexto.",
            },
        )
        db.add(delivery)
        await db.commit()
        return delivery.id, report.id


async def test_concurrent_acknowledgements_keep_a_single_first_receipt(
    audit_pg_factory,
):
    factory = audit_pg_factory
    token = f"race-ack-{uuid4().hex}"
    delivery_id, _ = await _seed_school_delivery(factory, token=token)

    async def acknowledge(name: str, role: str):
        body = ReportReceiptCreate(
            received=True, receiver_name=name, receiver_role=role
        )
        async with factory() as db:
            receipt = await school_report_delivery_service.acknowledge_school_delivery(
                db, token=token, body=body
            )
            await db.commit()
            return receipt

    first, second = await asyncio.wait_for(
        asyncio.gather(
            acknowledge("Ana", "Coordenação"), acknowledge("Bruno", "Direção")
        ),
        timeout=10,
    )
    assert first.received_at == second.received_at

    async with factory() as db:
        row = await db.get(ReportDelivery, delivery_id)
        assert row.received_at is not None
        assert row.received_at == first.received_at
        assert row.received_by_name in {"Ana", "Bruno"}
        assert row.received_by_role in {"Coordenação", "Direção"}


async def test_acknowledgement_and_revocation_serialize_on_the_delivery(
    audit_pg_factory,
):
    factory = audit_pg_factory
    token = f"race-revoke-{uuid4().hex}"
    delivery_id, report_id = await _seed_school_delivery(factory, token=token)
    body = ReportReceiptCreate(
        received=True, receiver_name="Ana", receiver_role="Coordenação"
    )

    async def acknowledge():
        async with factory() as db:
            try:
                receipt = await school_report_delivery_service.acknowledge_school_delivery(
                    db, token=token, body=body
                )
            except InvalidReportDeliveryToken:
                await db.rollback()
                return None
            await db.commit()
            return receipt

    async def revoke():
        async with factory() as db:
            await report_delivery_service.revoke_report_delivery(
                db, report_id=report_id, delivery_id=delivery_id
            )
            await db.commit()

    receipt, _ = await asyncio.wait_for(
        asyncio.gather(acknowledge(), revoke()), timeout=10
    )

    async with factory() as db:
        row = await db.get(ReportDelivery, delivery_id)
        assert row.revoked_at is not None
        if receipt is None:
            assert row.received_at is None
        else:
            assert row.received_at is not None
            assert row.received_at == receipt.received_at
