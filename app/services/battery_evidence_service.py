"""Battery session evidences and temporal events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assessment import Assessment
from app.models.attachment import Attachment
from app.models.battery_evidence import BatteryItemEvidence, BatterySessionEvent
from app.services.attachment_upload import (
    assert_declared_matches_sniff,
    normalize_content_type,
    sanitize_filename,
    sniff_content_type,
)
from app.services.storage import storage_service

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024

EVIDENCE_SIZE_LIMITS: dict[str, int] = {
    "photo": 10 * 1024 * 1024,
    "video": 200 * 1024 * 1024,
    "audio": 50 * 1024 * 1024,
}

EVIDENCE_CONTENT_TYPES: dict[str, set[str]] = {
    "photo": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "audio": {"audio/mpeg", "audio/wav", "audio/webm", "audio/ogg", "audio/mp4"},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BatteryEvidenceService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _load_battery(self, battery_id: UUID, *, professional_id: UUID) -> Assessment:
        record = (
            await self.db.execute(
                select(Assessment)
                .options(selectinload(Assessment.patient))
                .where(Assessment.id == battery_id, Assessment.professional_id == professional_id)
            )
        ).scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail="Bateria não encontrada")
        return record

    def _to_evidence_dict(self, evidence: BatteryItemEvidence) -> dict[str, Any]:
        return {
            "id": str(evidence.id),
            "battery_id": str(evidence.battery_id),
            "subform_slug": evidence.subform_slug,
            "item_id": evidence.item_id,
            "attachment_id": str(evidence.attachment_id) if evidence.attachment_id else None,
            "kind": evidence.kind,
            "note_text": evidence.note_text,
            "recorded_at": evidence.recorded_at.isoformat(),
            "created_at": evidence.created_at.isoformat(),
        }

    def _to_event_dict(self, event: BatterySessionEvent) -> dict[str, Any]:
        return {
            "id": str(event.id),
            "battery_id": str(event.battery_id),
            "occurred_at": event.occurred_at.isoformat(),
            "text": event.text,
            "subform_slug": event.subform_slug,
            "item_id": event.item_id,
            "evidence_id": str(event.evidence_id) if event.evidence_id else None,
            "created_at": event.created_at.isoformat(),
        }

    async def list_evidences(
        self,
        battery_id: UUID,
        *,
        professional_id: UUID,
        subform_slug: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        await self._load_battery(battery_id, professional_id=professional_id)
        query = select(BatteryItemEvidence).where(BatteryItemEvidence.battery_id == battery_id)
        if subform_slug:
            query = query.where(BatteryItemEvidence.subform_slug == subform_slug)
        if item_id:
            query = query.where(BatteryItemEvidence.item_id == item_id)
        query = query.order_by(BatteryItemEvidence.recorded_at.asc())
        rows = list((await self.db.execute(query)).scalars().all())
        return [self._to_evidence_dict(row) for row in rows]

    async def create_note_evidence(
        self,
        battery_id: UUID,
        *,
        professional_id: UUID,
        kind: str,
        note_text: str,
        subform_slug: Optional[str] = None,
        item_id: Optional[str] = None,
        recorded_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if kind != "note":
            raise HTTPException(status_code=400, detail="Use upload para mídia")
        battery = await self._load_battery(battery_id, professional_id=professional_id)
        evidence = BatteryItemEvidence(
            battery_id=battery_id,
            subform_slug=subform_slug,
            item_id=item_id,
            kind="note",
            note_text=note_text,
            recorded_at=recorded_at or _utcnow(),
            created_by=professional_id,
        )
        self.db.add(evidence)
        await self.db.commit()
        await self.db.refresh(evidence)
        return self._to_evidence_dict(evidence)

    async def upload_evidence(
        self,
        battery_id: UUID,
        *,
        professional_id: UUID,
        file: UploadFile,
        kind: str,
        subform_slug: Optional[str] = None,
        item_id: Optional[str] = None,
        recorded_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if kind not in EVIDENCE_SIZE_LIMITS:
            raise HTTPException(status_code=400, detail="Tipo de evidência inválido")
        battery = await self._load_battery(battery_id, professional_id=professional_id)
        if not battery.patient_id:
            raise HTTPException(status_code=400, detail="Paciente não encontrado")

        limit = EVIDENCE_SIZE_LIMITS[kind]
        chunks: list[bytes] = []
        total_read = 0
        while True:
            chunk = await file.read(UPLOAD_READ_CHUNK_SIZE)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > limit:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Arquivo excede limite de {limit // (1024 * 1024)} MB",
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Arquivo vazio não é permitido.",
            )

        content_type = normalize_content_type(file.content_type)
        allowed = EVIDENCE_CONTENT_TYPES.get(kind, set())
        if not content_type:
            sniffed = sniff_content_type(body)
            if sniffed and sniffed in allowed:
                content_type = sniffed
        if allowed and content_type not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Content-type não permitido: {content_type or (file.content_type or 'desconhecido')}",
            )

        assert_declared_matches_sniff(content_type, body)
        filename = sanitize_filename(file.filename or f"{kind}-upload")
        key = storage_service.make_key(battery.patient_id, filename)
        await storage_service.upload(key, body, content_type)

        attachment = Attachment(
            patient_id=battery.patient_id,
            professional_id=professional_id,
            name=filename,
            category=f"battery_{kind}",
            size_bytes=len(body),
            storage_key=key,
            date=recorded_at or _utcnow(),
        )
        self.db.add(attachment)
        await self.db.flush()

        evidence = BatteryItemEvidence(
            battery_id=battery_id,
            subform_slug=subform_slug,
            item_id=item_id,
            attachment_id=attachment.id,
            kind=kind,
            recorded_at=recorded_at or _utcnow(),
            created_by=professional_id,
        )
        self.db.add(evidence)
        await self.db.commit()
        await self.db.refresh(evidence)
        result = self._to_evidence_dict(evidence)
        result["url"] = await storage_service.presigned_url(
            key,
            filename=attachment.name,
            as_attachment=False,
        )
        return result

    async def delete_evidence(
        self,
        battery_id: UUID,
        evidence_id: UUID,
        *,
        professional_id: UUID,
    ) -> None:
        await self._load_battery(battery_id, professional_id=professional_id)
        evidence = (
            await self.db.execute(
                select(BatteryItemEvidence).where(
                    BatteryItemEvidence.id == evidence_id,
                    BatteryItemEvidence.battery_id == battery_id,
                )
            )
        ).scalar_one_or_none()
        if not evidence:
            raise HTTPException(status_code=404, detail="Evidência não encontrada")
        await self.db.delete(evidence)
        await self.db.commit()

    async def get_evidence_url(
        self,
        battery_id: UUID,
        evidence_id: UUID,
        *,
        professional_id: UUID,
    ) -> str:
        await self._load_battery(battery_id, professional_id=professional_id)
        evidence = (
            await self.db.execute(
                select(BatteryItemEvidence)
                .options(selectinload(BatteryItemEvidence.attachment))
                .where(
                    BatteryItemEvidence.id == evidence_id,
                    BatteryItemEvidence.battery_id == battery_id,
                )
            )
        ).scalar_one_or_none()
        if not evidence or not evidence.attachment:
            raise HTTPException(status_code=404, detail="Evidência ou anexo não encontrado")
        return await storage_service.presigned_url(
            evidence.attachment.storage_key,
            filename=evidence.attachment.name,
            as_attachment=False,
        )

    async def list_events(
        self,
        battery_id: UUID,
        *,
        professional_id: UUID,
    ) -> list[dict[str, Any]]:
        await self._load_battery(battery_id, professional_id=professional_id)
        rows = list(
            (
                await self.db.execute(
                    select(BatterySessionEvent)
                    .where(BatterySessionEvent.battery_id == battery_id)
                    .order_by(BatterySessionEvent.occurred_at.asc())
                )
            ).scalars().all()
        )
        return [self._to_event_dict(row) for row in rows]

    async def create_event(
        self,
        battery_id: UUID,
        *,
        professional_id: UUID,
        text: str,
        occurred_at: Optional[datetime] = None,
        subform_slug: Optional[str] = None,
        item_id: Optional[str] = None,
        evidence_id: Optional[UUID] = None,
    ) -> dict[str, Any]:
        await self._load_battery(battery_id, professional_id=professional_id)
        event = BatterySessionEvent(
            battery_id=battery_id,
            text=text,
            occurred_at=occurred_at or _utcnow(),
            subform_slug=subform_slug,
            item_id=item_id,
            evidence_id=evidence_id,
        )
        self.db.add(event)
        await self.db.commit()
        await self.db.refresh(event)
        return self._to_event_dict(event)

    async def update_event(
        self,
        battery_id: UUID,
        event_id: UUID,
        *,
        professional_id: UUID,
        text: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        subform_slug: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        await self._load_battery(battery_id, professional_id=professional_id)
        event = (
            await self.db.execute(
                select(BatterySessionEvent).where(
                    BatterySessionEvent.id == event_id,
                    BatterySessionEvent.battery_id == battery_id,
                )
            )
        ).scalar_one_or_none()
        if not event:
            raise HTTPException(status_code=404, detail="Evento não encontrado")
        if text is not None:
            event.text = text
        if occurred_at is not None:
            event.occurred_at = occurred_at
        if subform_slug is not None:
            event.subform_slug = subform_slug
        if item_id is not None:
            event.item_id = item_id
        await self.db.commit()
        await self.db.refresh(event)
        return self._to_event_dict(event)

    async def delete_event(
        self,
        battery_id: UUID,
        event_id: UUID,
        *,
        professional_id: UUID,
    ) -> None:
        await self._load_battery(battery_id, professional_id=professional_id)
        event = (
            await self.db.execute(
                select(BatterySessionEvent).where(
                    BatterySessionEvent.id == event_id,
                    BatterySessionEvent.battery_id == battery_id,
                )
            )
        ).scalar_one_or_none()
        if not event:
            raise HTTPException(status_code=404, detail="Evento não encontrado")
        await self.db.delete(event)
        await self.db.commit()
