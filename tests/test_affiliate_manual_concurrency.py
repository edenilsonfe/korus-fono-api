"""Real PostgreSQL races for manual affiliate attribution."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.affiliate import (
    AffiliateCode,
    AffiliateParticipant,
    AffiliatePolicy,
    AffiliateReferral,
)
from app.models.feature_flag import FeatureFlag
from app.models.professional import Professional
from app.services.affiliate_notification_service import AffiliateNotificationService
from app.services.affiliate_service import AffiliateConflictError, AffiliateService

pytestmark = pytest.mark.asyncio


async def _run_referral_race(factory, calls):
    barrier = asyncio.Barrier(len(calls))

    async def run(code, professional_id):
        async with factory() as db:
            await barrier.wait()
            professional = await db.get(Professional, professional_id)
            try:
                referral = await AffiliateService(db).register_checkout_referral(
                    code=code,
                    referred_professional=professional,
                    request_ip="203.0.113.10",
                    user_agent="affiliate-concurrency-test",
                )
                await db.commit()
                return "ok", referral.id
            except AffiliateConflictError as exc:
                await db.rollback()
                return "conflict", str(exc)

    tasks = [asyncio.create_task(run(code, professional_id)) for code, professional_id in calls]
    try:
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _seed_partner_codes(factory):
    async with factory() as db:
        now = datetime.now(UTC)
        target = Professional(
            email="manual-race-target@example.com",
            password_hash="unused",
            name="Manual Race Target",
            subscription_status="trialing",
            trial_ends_at=now + timedelta(days=7),
            email_verified_at=now,
        )
        policy = AffiliatePolicy(
            mode="partner",
            version=1,
            status="active",
            terms_version="v1",
            referral_discount_bps=1000,
            effective_at=now,
        )
        flag = FeatureFlag(
            key="affiliate_partner_program",
            description="manual concurrency test",
            enabled_global=True,
        )
        participants = [
            AffiliateParticipant(
                email="manual-race-partner-a@example.com",
                status="active",
                partner_enabled=True,
                partner_terms_version="v1",
            ),
            AffiliateParticipant(
                email="manual-race-partner-b@example.com",
                status="active",
                partner_enabled=True,
                partner_terms_version="v1",
            ),
        ]
        db.add_all([target, policy, flag, *participants])
        await db.flush()
        codes = [
            AffiliateCode(
                participant_id=participant.id,
                mode="partner",
                code=f"manualrace{suffix}",
                status="active",
                terms_version="v1",
            )
            for participant, suffix in zip(participants, ("a1234", "b1234"), strict=True)
        ]
        db.add_all(codes)
        await db.commit()
        return target.id, [code.code for code in codes]


async def _seed_reciprocal_customer_codes(factory):
    async with factory() as db:
        now = datetime.now(UTC)
        professionals = [
            Professional(
                email="reciprocal-a@example.com",
                password_hash="unused",
                name="Reciprocal A",
                subscription_status="trialing",
                trial_ends_at=now + timedelta(days=7),
                email_verified_at=now,
            ),
            Professional(
                email="reciprocal-b@example.com",
                password_hash="unused",
                name="Reciprocal B",
                subscription_status="trialing",
                trial_ends_at=now + timedelta(days=7),
                email_verified_at=now,
            ),
        ]
        policy = AffiliatePolicy(
            mode="customer",
            version=1,
            status="active",
            terms_version="v1",
            referral_discount_bps=1000,
            effective_at=now,
        )
        flag = FeatureFlag(
            key="affiliate_customer_program",
            description="reciprocal concurrency test",
            enabled_global=True,
        )
        db.add_all([*professionals, policy, flag])
        await db.flush()
        participants = [
            AffiliateParticipant(
                professional_id=professional.id,
                email=professional.email,
                status="active",
                customer_enabled=True,
                customer_terms_version="v1",
            )
            for professional in professionals
        ]
        db.add_all(participants)
        await db.flush()
        codes = [
            AffiliateCode(
                participant_id=participant.id,
                mode="customer",
                code=f"reciprocal{suffix}",
                status="active",
                terms_version="v1",
            )
            for participant, suffix in zip(participants, ("a1234", "b1234"), strict=True)
        ]
        db.add_all(codes)
        await db.commit()
        return [professional.id for professional in professionals], [code.code for code in codes]


async def test_two_codes_for_one_account_create_one_referral_and_conflict(audit_pg_factory):
    professional_id, codes = await _seed_partner_codes(audit_pg_factory)

    results = await _run_referral_race(
        audit_pg_factory,
        [(codes[0], professional_id), (codes[1], professional_id)],
    )

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    async with audit_pg_factory() as db:
        referrals = (await db.scalars(select(AffiliateReferral))).all()
        assert len(referrals) == 1
        assert referrals[0].code_id in {
            code_id
            for code_id in await db.scalars(
                select(AffiliateCode.id).where(AffiliateCode.code.in_(codes))
            )
        }


async def test_reciprocal_trial_referrals_complete_without_deadlock(audit_pg_factory, monkeypatch):
    professional_ids, codes = await _seed_reciprocal_customer_codes(audit_pg_factory)
    notifications_ready = asyncio.Barrier(2)
    original_notify = AffiliateNotificationService.notify

    async def simultaneous_notify(self, **kwargs):
        # Both transactions hold their account lock before checking the other
        # account's notification FK. FOR UPDATE would deadlock here.
        await notifications_ready.wait()
        await original_notify(self, **kwargs)

    monkeypatch.setattr(AffiliateNotificationService, "notify", simultaneous_notify)

    results = await _run_referral_race(
        audit_pg_factory,
        [(codes[0], professional_ids[1]), (codes[1], professional_ids[0])],
    )

    assert [result[0] for result in results] == ["ok", "ok"]
    async with audit_pg_factory() as db:
        referrals = (await db.scalars(select(AffiliateReferral))).all()
        assert len(referrals) == 2
        assert {referral.referred_professional_id for referral in referrals} == set(professional_ids)
