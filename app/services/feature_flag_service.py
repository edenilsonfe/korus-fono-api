from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Subscription
from app.models.feature_flag import FeatureFlag, FeatureFlagOverride
from app.models.professional import Professional
from app.schemas.admin_product import (
    FeatureFlagCreate,
    FeatureFlagItem,
    FeatureFlagUpdate,
    ProfessionalFlagState,
)
from app.services.admin_audit_service import AdminAuditService


class FeatureFlagNotFoundError(Exception):
    pass


class FeatureFlagConflictError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class FeatureFlagService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.audit = AdminAuditService(db)

    async def is_enabled(self, professional: Professional, key: str) -> bool:
        flag = await self.db.get(FeatureFlag, key)
        if flag is None:
            return False

        override = await self.db.get(
            FeatureFlagOverride, {"flag_key": key, "professional_id": professional.id}
        )
        if override is not None:
            return override.enabled

        if flag.audience:
            if not await self._matches_audience(professional, flag.audience):
                return False

        return flag.enabled_global

    async def list_flags(self) -> list[FeatureFlagItem]:
        result = await self.db.execute(select(FeatureFlag).order_by(FeatureFlag.key.asc()))
        return [self._to_item(f) for f in result.scalars().all()]

    async def create_flag(
        self, *, actor: Professional, body: FeatureFlagCreate
    ) -> FeatureFlagItem:
        existing = await self.db.get(FeatureFlag, body.key)
        if existing:
            raise FeatureFlagConflictError("Flag já existe")
        flag = FeatureFlag(
            key=body.key,
            description=body.description,
            enabled_global=body.enabled_global,
            audience=body.audience,
        )
        self.db.add(flag)
        await self.audit.log(
            actor_id=actor.id,
            action="create_feature_flag",
            payload={"key": body.key, "reason": body.reason},
        )
        await self.db.commit()
        await self.db.refresh(flag)
        return self._to_item(flag)

    async def update_flag(
        self, *, actor: Professional, key: str, body: FeatureFlagUpdate
    ) -> FeatureFlagItem:
        flag = await self.db.get(FeatureFlag, key)
        if flag is None:
            raise FeatureFlagNotFoundError()
        before = {
            "description": flag.description,
            "enabled_global": flag.enabled_global,
            "audience": flag.audience,
        }
        if body.description is not None:
            flag.description = body.description
        if body.enabled_global is not None:
            flag.enabled_global = body.enabled_global
        if body.clear_audience:
            flag.audience = None
        elif body.audience is not None:
            flag.audience = body.audience
        await self.audit.log(
            actor_id=actor.id,
            action="update_feature_flag",
            payload={
                "key": key,
                "before": before,
                "after": {
                    "description": flag.description,
                    "enabled_global": flag.enabled_global,
                    "audience": flag.audience,
                },
                "reason": body.reason,
            },
        )
        await self.db.commit()
        await self.db.refresh(flag)
        return self._to_item(flag)

    async def list_professional_flags(
        self, professional_id: UUID
    ) -> list[ProfessionalFlagState]:
        pro = await self.db.get(Professional, professional_id)
        if pro is None:
            raise FeatureFlagNotFoundError()
        flags = (await self.db.execute(select(FeatureFlag).order_by(FeatureFlag.key))).scalars().all()
        overrides = (
            await self.db.execute(
                select(FeatureFlagOverride).where(
                    FeatureFlagOverride.professional_id == professional_id
                )
            )
        ).scalars().all()
        override_map = {o.flag_key: o.enabled for o in overrides}
        states: list[ProfessionalFlagState] = []
        for flag in flags:
            override = override_map.get(flag.key)
            resolved = await self.is_enabled(pro, flag.key)
            states.append(
                ProfessionalFlagState(
                    key=flag.key,
                    description=flag.description,
                    enabled_global=flag.enabled_global,
                    override=override,
                    resolved=resolved,
                )
            )
        return states

    async def set_override(
        self,
        *,
        actor: Professional,
        professional_id: UUID,
        key: str,
        enabled: bool | None,
        reason: str | None,
    ) -> list[ProfessionalFlagState]:
        flag = await self.db.get(FeatureFlag, key)
        if flag is None:
            raise FeatureFlagNotFoundError()
        pro = await self.db.get(Professional, professional_id)
        if pro is None:
            raise FeatureFlagNotFoundError()

        existing = await self.db.get(
            FeatureFlagOverride, {"flag_key": key, "professional_id": professional_id}
        )
        if enabled is None:
            if existing:
                await self.db.delete(existing)
            action = "clear_feature_flag_override"
            payload: dict[str, Any] = {"key": key, "reason": reason}
        else:
            if existing:
                existing.enabled = enabled
            else:
                self.db.add(
                    FeatureFlagOverride(
                        flag_key=key, professional_id=professional_id, enabled=enabled
                    )
                )
            action = "set_feature_flag_override"
            payload = {"key": key, "enabled": enabled, "reason": reason}

        await self.audit.log(
            actor_id=actor.id,
            target_professional_id=professional_id,
            action=action,
            payload=payload,
        )
        await self.db.commit()
        return await self.list_professional_flags(professional_id)

    async def _matches_audience(self, professional: Professional, audience: dict[str, Any]) -> bool:
        specialty_keys = audience.get("specialtyKeys") or audience.get("specialty_keys") or []
        plan_slugs = audience.get("planSlugs") or audience.get("plan_slugs") or []
        if specialty_keys and professional.specialty_key not in specialty_keys:
            return False
        if plan_slugs:
            sub = (
                await self.db.execute(
                    select(Subscription)
                    .options(selectinload(Subscription.plan))
                    .where(Subscription.professional_id == professional.id)
                    .order_by(Subscription.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            slug = sub.plan.slug if sub and sub.plan else None
            if slug not in plan_slugs:
                return False
        return True

    @staticmethod
    def _to_item(flag: FeatureFlag) -> FeatureFlagItem:
        return FeatureFlagItem(
            key=flag.key,
            description=flag.description,
            enabled_global=flag.enabled_global,
            audience=flag.audience,
        )
