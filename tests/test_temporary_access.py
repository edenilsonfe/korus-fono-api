"""Admin grants never impersonate a payment; the API enforces their deadline."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.deps import require_verified_professional
from app.core.security import create_access_token, hash_password
from app.models.admin_audit_log import AdminAuditLog
from app.models.billing import Plan, Subscription
from app.models.professional import Professional
from app.services.entitlement_service import EntitlementService
from app.services.temporary_access import has_temporary_access


def headers(pro):
    return {"Authorization": f"Bearer {create_access_token(pro.id, pro.token_version)}"}


@pytest.fixture
async def staff(db_session, professional):
    professional.is_staff = True
    professional.admin_role = "billing"
    await db_session.commit()
    return professional


@pytest.fixture
async def pending(db_session):
    pro = Professional(
        email="temporary@example.com",
        name="Cliente pendente",
        password_hash=hash_password("testpass123"),
        subscription_status="active",
        signup_payment_required=True,
    )
    plan = Plan(
        slug="temporary-monthly",
        name="Mensal",
        billing_interval="monthly",
        price_cents=9790,
    )
    db_session.add_all([pro, plan])
    await db_session.flush()
    sub = Subscription(
        professional_id=pro.id,
        plan_id=plan.id,
        provider="asaas",
        status="incomplete",
        external_checkout_id="pay_pending",
        checkout_charge_cents=9790,
    )
    db_session.add(sub)
    await db_session.commit()
    return pro, sub


@pytest.fixture(autouse=True)
def isolated_middleware(monkeypatch, db_engine):
    monkeypatch.setattr(
        "app.middleware.entitlement.AsyncSessionLocal",
        async_sessionmaker(db_engine, expire_on_commit=False),
    )


async def grant(client, actor, target, body=None):
    return await client.post(
        f"/api/v1/admin/professionals/{target.id}/temporary-access",
        headers=headers(actor),
        json=body or {"reason": "Instabilidade no Pix"},
    )


async def test_grant_default_48h_preserves_billing_and_audits(
    api_client, db_session, staff, pending
):
    pro, sub = pending
    response = await grant(api_client, staff, pro)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["temporaryAccessActive"] is True
    assert body["signupPaymentRequired"] is True
    assert body["emailVerified"] is False
    await db_session.refresh(pro)
    await db_session.refresh(sub)
    remaining = pro.temporary_access_ends_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(hours=47, minutes=59) < remaining <= timedelta(hours=48)
    assert pro.subscription_status == "active"
    assert pro.signup_payment_required is True
    assert pro.email_verified_at is None
    assert pro.trial_started_at is None and pro.trial_ends_at is None
    assert sub.status == "incomplete" and sub.external_checkout_id == "pay_pending"
    assert sub.last_payment_at is None and sub.current_period_end is None
    event = (await db_session.execute(select(AdminAuditLog))).scalar_one()
    assert event.actor_id == staff.id and event.target_professional_id == pro.id
    assert event.action == "grant_temporary_access"
    assert (
        event.payload["days"] == 2 and event.payload["reason"] == "Instabilidade no Pix"
    )
    assert await EntitlementService(db_session).can_write(pro)
    me = await api_client.get("/api/v1/me", headers=headers(pro))
    assert me.json()["signupPaymentRequired"] is True
    assert me.json()["temporaryAccessEndsAt"]


async def test_repeated_grant_does_not_extend_and_revoke_is_idempotent(
    api_client, db_session, staff, pending
):
    pro, _ = pending
    first = await grant(api_client, staff, pro)
    assert first.status_code == 200
    second = await grant(
        api_client, staff, pro, {"days": 7, "reason": "Nova tentativa"}
    )
    assert second.status_code == 409
    await db_session.refresh(pro)
    assert pro.temporary_access_ends_at.isoformat() == first.json()[
        "temporaryAccessEndsAt"
    ].replace("Z", "+00:00").removesuffix("+00:00")
    for _ in range(2):
        revoked = await api_client.post(
            f"/api/v1/admin/professionals/{pro.id}/temporary-access/revoke",
            headers=headers(staff),
            json={"reason": "Atendimento encerrado"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["temporaryAccessEndsAt"] is None
    await db_session.refresh(pro)
    assert not await EntitlementService(db_session).can_write(pro)
    logs = (await db_session.execute(select(AdminAuditLog))).scalars().all()
    assert [log.action for log in logs] == [
        "grant_temporary_access",
        "revoke_temporary_access",
    ]


@pytest.mark.parametrize("days", [0, 8, -1, 2.5, True, "2"])
async def test_invalid_duration_rejected(api_client, db_session, staff, pending, days):
    pro, _ = pending
    response = await grant(
        api_client, staff, pro, {"days": days, "reason": "Instabilidade no Pix"}
    )
    assert response.status_code == 422
    await db_session.refresh(pro)
    assert pro.temporary_access_ends_at is None


@pytest.mark.parametrize("reason", ["     ", "  abc  ", "x" * 501])
async def test_invalid_reason_rejected(api_client, staff, pending, reason):
    assert (
        await grant(api_client, staff, pending[0], {"reason": reason})
    ).status_code == 422


@pytest.mark.parametrize("role", ["support", "product", None])
async def test_grant_and_revoke_require_billing_permission(
    api_client, db_session, staff, pending, role
):
    staff.admin_role = role
    staff.is_staff = role is not None
    await db_session.commit()
    pro, _ = pending
    assert (await grant(api_client, staff, pro)).status_code == 403
    revoke = await api_client.post(
        f"/api/v1/admin/professionals/{pro.id}/temporary-access/revoke",
        headers=headers(staff),
        json={"reason": "Tentativa sem permissão"},
    )
    assert revoke.status_code == 403
    assert not (await db_session.execute(select(AdminAuditLog))).scalars().all()


@pytest.mark.parametrize("disabled,already_paid", [(True, False), (False, True)])
async def test_disabled_or_normally_active_account_rejected(
    api_client, db_session, staff, pending, disabled, already_paid
):
    pro, _ = pending
    pro.is_disabled = disabled
    pro.signup_payment_required = not already_paid
    await db_session.commit()
    assert (await grant(api_client, staff, pro)).status_code == 409


async def test_verification_flow_and_expiry_enforced_on_reads_and_writes(
    api_client, db_session, staff, pending, monkeypatch
):
    pro, _ = pending
    send = AsyncMock()
    monkeypatch.setattr("app.api.v1.billing.get_payment_gateway", lambda: None)
    monkeypatch.setattr("app.api.v1.auth.send_email_verification_email_task", send)
    assert (await grant(api_client, staff, pro)).status_code == 200
    res = await api_client.get("/api/v1/patients", headers=headers(pro))
    assert res.status_code == 403 and res.json()["detail"] == "E-mail não verificado"
    res = await api_client.post(
        "/api/v1/auth/resend-verification", headers=headers(pro)
    )
    assert res.status_code == 200, res.text
    send.assert_awaited_once()
    token = send.call_args.args[2]
    res = await api_client.post("/api/v1/auth/verify-email", json={"token": token})
    assert res.status_code == 200, res.text
    assert (
        await api_client.get("/api/v1/patients", headers=headers(pro))
    ).status_code == 200
    created = await api_client.post(
        "/api/v1/patients",
        headers=headers(pro),
        json={
            "name": "Paciente teste",
            "birthDate": "2020-01-01",
            "diagnosisKeys": ["tea"],
        },
    )
    assert created.status_code == 201, created.text
    billing = (await api_client.get("/api/v1/billing/me", headers=headers(pro))).json()
    assert billing["canWrite"] is True and billing["signupPaymentRequired"] is True
    assert billing["temporaryAccessEndsAt"]
    assert billing["subscription"]["status"] == "incomplete"
    pro.temporary_access_ends_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert (
        await api_client.get("/api/v1/patients", headers=headers(pro))
    ).status_code == 403
    blocked = await api_client.post(
        "/api/v1/patients",
        headers=headers(pro),
        json={"name": "Bloqueado", "birthDate": "2020-01-01"},
    )
    assert blocked.status_code == 403 and blocked.json()["type"] == "entitlement_error"
    billing = (await api_client.get("/api/v1/billing/me", headers=headers(pro))).json()
    assert billing["canWrite"] is False and billing["signupPaymentRequired"] is True
    assert (
        await api_client.post("/api/v1/auth/resend-verification", headers=headers(pro))
    ).status_code == 403


async def test_expiry_boundary_and_disabled_account_do_not_bypass_security(
    db_session, pending
):
    pro, _ = pending
    now = datetime.now(UTC)
    pro.temporary_access_ends_at = now
    assert not has_temporary_access(pro, now=now)
    pro.temporary_access_ends_at = now + timedelta(seconds=1)
    assert has_temporary_access(pro, now=now)
    with pytest.raises(HTTPException) as exc:
        await require_verified_professional(pro)
    assert exc.value.detail == "E-mail não verificado"
    pro.is_disabled = True
    assert not await EntitlementService(db_session).can_write(pro)


async def test_late_billing_failure_does_not_end_grace(db_session, pending):
    pro, _ = pending
    pro.temporary_access_ends_at = datetime.now(UTC) + timedelta(days=2)
    pro.subscription_status = "past_due"
    await db_session.commit()
    assert await EntitlementService(db_session).can_write(pro)
    pro.temporary_access_ends_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not await EntitlementService(db_session).can_write(pro)


async def test_login_during_grace_requests_email_verification(
    api_client, db_session, pending, monkeypatch
):
    pro, _ = pending
    pro.temporary_access_ends_at = datetime.now(UTC) + timedelta(days=2)
    await db_session.commit()
    request = AsyncMock(return_value="test-verification-token")
    send = AsyncMock()
    monkeypatch.setattr("app.api.v1.auth.enforce_login_rate_limit", lambda *_: None)
    monkeypatch.setattr("app.api.v1.auth.request_email_verification", request)
    monkeypatch.setattr("app.api.v1.auth.send_email_verification_email_task", send)
    response = await api_client.post(
        "/api/v1/auth/login", json={"email": pro.email, "password": "testpass123"}
    )
    assert response.status_code == 200, response.text
    request.assert_awaited_once()
    send.assert_awaited_once_with(pro.email, pro.name, "test-verification-token")


async def test_expired_grace_restores_expired_trial_from_database(db_session, pending):
    pro, _ = pending
    pro.signup_payment_required = False
    pro.subscription_status = "trialing"
    pro.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
    pro.temporary_access_ends_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await db_session.refresh(pro)
    assert not await EntitlementService(db_session).can_write(pro)
    assert pro.subscription_status == "trial_expired"
