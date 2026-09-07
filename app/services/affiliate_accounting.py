"""Common lock for every affiliate balance mutation (held until commit)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate import AffiliateParticipant


async def lock_participant(
    db: AsyncSession, participant_id: UUID
) -> AffiliateParticipant | None:
    return (
        await db.execute(
            select(AffiliateParticipant)
            .where(AffiliateParticipant.id == participant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
