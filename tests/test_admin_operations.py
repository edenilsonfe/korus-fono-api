from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.models.admin_audit_log import AdminAuditLog
from app.models.ai import AIJob
from app.models.billing import Plan, Subscription
from app.models.platform_whatsapp_connection import PlatformWhatsAppConnection
from app.models.professional import Professional
from app.models.trial_email_campaign import TrialEmailCampaign

pytestmark = pytest.mark.asyncio


async def _professional(db, email: str, *, role: str | None = None, is_staff: bool = False):
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=email,
        specialty="Fonoaudiologia",
        specialty_key="fono",
        email_verified_at=datetime.now(UTC),
        is_staff=is_staff or role is not None,
        admin_role=role,
    )
    db.add(professional)
    await db.commit()
    await db.refresh(professional)
    return professional


def _headers(professional: Professional) -> dict[str, str]:
    token = create_access_token(professional.id, professional.token_version)
    return {"Authorization": f"Bearer {token}"}


async def test_support_can_read_accounts_but_cannot_mutate_them(api_client, db_session):
    support = await _professional(db_session, "support@korus.test", role="support")
    target = await _professional(db_session, "target@korus.test")

    response = await api_client.get(
        "/api/v1/admin/professionals",
        headers=_headers(support),
    )
    assert response.status_code == 200

    response = await api_client.post(
        f"/api/v1/admin/professionals/{target.id}/extend-trial",
        headers=_headers(support),
        json={"days": 7, "reason": "Solicitacao de suporte documentada"},
    )
    assert response.status_code == 403


async def test_only_superadmin_can_change_an_admin_role(api_client, db_session):
    superadmin = await _professional(db_session, "root@korus.test", role="superadmin")
    billing = await _professional(db_session, "billing@korus.test", role="billing")
    target = await _professional(db_session, "new-support@korus.test")
    payload = {"adminRole": "support", "reason": "Entrada no time de suporte"}

    denied = await api_client.patch(
        f"/api/v1/admin/professionals/{target.id}/admin-role",
        headers=_headers(billing),
        json=payload,
    )
    assert denied.status_code == 403

    allowed = await api_client.patch(
        f"/api/v1/admin/professionals/{target.id}/admin-role",
        headers=_headers(superadmin),
        json=payload,
    )
    assert allowed.status_code == 200
    assert allowed.json()["adminRole"] == "support"


async def test_audit_feed_is_paginated_and_redacts_sensitive_payloads(api_client, db_session):
    superadmin = await _professional(db_session, "audit@korus.test", role="superadmin")
    target = await _professional(db_session, "audited@korus.test")
    db_session.add(
        AdminAuditLog(
            actor_id=superadmin.id,
            actor_name=superadmin.name,
            actor_email=superadmin.email,
            target_professional_id=target.id,
            action="test_sensitive_action",
            payload={
                "reason": "Teste",
                "api_key": "must-not-leak",
                "authorization": "Bearer must-not-leak",
            },
        )
    )
    await db_session.commit()

    response = await api_client.get(
        f"/api/v1/admin/audit-events?targetProfessionalId={target.id}",
        headers=_headers(superadmin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["actorEmail"] == superadmin.email
    assert body["items"][0]["payload"]["api_key"] == "[REDACTED]"
    assert body["items"][0]["payload"]["authorization"] == "[REDACTED]"


async def test_attention_queue_aggregates_only_operational_metadata(api_client, db_session):
    superadmin = await _professional(db_session, "ops@korus.test", role="superadmin")
    target = await _professional(db_session, "divergent@korus.test")
    target.subscription_status = "active"
    plan = Plan(
        slug="attention-plan",
        name="Attention Plan",
        price_cents=9700,
        billing_interval="monthly",
    )
    db_session.add(plan)
    await db_session.flush()
    db_session.add(
        Subscription(
            professional_id=target.id,
            plan_id=plan.id,
            status="past_due",
            provider="asaas",
        )
    )
    db_session.add(
        TrialEmailCampaign(
            actor_id=superadmin.id,
            audience="expired",
            status="failed",
            eligible_count=1,
            error="provider unavailable",
        )
    )
    db_session.add(
        AIJob(
            professional_id=target.id,
            job_type="report",
            status="processing",
            input_hash="stuck-job",
            input_data="{}",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db_session.add(PlatformWhatsAppConnection(status="not_connected"))
    await db_session.commit()

    response = await api_client.get(
        "/api/v1/admin/attention",
        headers=_headers(superadmin),
    )

    assert response.status_code == 200
    body = response.json()
    kinds = {item["kind"] for item in body["items"]}
    assert {"billing_divergence", "trial_email_failed", "ai_job_stuck", "whatsapp_disconnected"} <= kinds
    assert all("patient" not in str(item).lower() for item in body["items"])


async def test_role_change_is_recorded_with_actor_snapshot(api_client, db_session):
    superadmin = await _professional(db_session, "snapshot@korus.test", role="superadmin")
    target = await _professional(db_session, "snapshot-target@korus.test")

    response = await api_client.patch(
        f"/api/v1/admin/professionals/{target.id}/admin-role",
        headers=_headers(superadmin),
        json={"adminRole": "product", "reason": "Responsavel pelo catalogo"},
    )
    assert response.status_code == 200

    result = await db_session.execute(
        select(AdminAuditLog).where(AdminAuditLog.action == "set_admin_role")
    )
    event = result.scalar_one()
    assert event.actor_name == superadmin.name
    assert event.actor_email == superadmin.email
    assert event.payload["reason"] == "Responsavel pelo catalogo"
