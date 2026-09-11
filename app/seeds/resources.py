"""Seed global resources catalog into DB + object storage."""

import hashlib
import logging
import uuid

from sqlalchemy import select

from app.models.resource import Resource
from app.seeds.resources_data import GLOBAL_RESOURCE_SEED
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


async def seed_resources(session) -> None:
    existing = await session.execute(
        select(Resource.id).where(Resource.owner_professional_id.is_(None)).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return

    for item in GLOBAL_RESOURCE_SEED:
        resource_id = uuid.uuid4()
        filename = item["filename"]
        storage_key = storage_service.make_resource_key(resource_id, filename)
        file_bytes = item["file_bytes"]
        try:
            await storage_service.upload(storage_key, file_bytes, item["content_type"])
        except Exception:
            # Conservador: sem arquivo no storage a linha não é persistida
            # (nem como rascunho órfão). Seed tolera MinIO ausente em dev.
            logger.warning(
                "seed_resources: upload falhou para %s; item não será inserido", filename
            )
            continue

        session.add(
            Resource(
                id=resource_id,
                owner_professional_id=None,
                title=item["title"],
                description=item["description"],
                categories=item["categories"],
                format=item["format"],
                file_size_bytes=len(file_bytes),
                pages=item.get("pages"),
                author=item["author"],
                storage_key=storage_key,
                content_type=item["content_type"],
                downloads=item.get("downloads", 0),
                featured=item.get("featured", False),
                accent=item.get("accent", "primary"),
                objective=item.get("objective"),
                age_range=item.get("age_range"),
                skill=item.get("skill"),
                related_protocol=item.get("related_protocol"),
                difficulty=item.get("difficulty"),
                content_sha256=hashlib.sha256(file_bytes).hexdigest(),
                # Placeholders do seed não são publicados nem licenciados:
                # curadoria/liberação é gate operacional separado (F17).
                publication_status="draft",
            )
        )
