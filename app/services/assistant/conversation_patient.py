"""Bind optional patientId from a chat message onto a conversation (set-if-empty)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import Conversation
from app.models.professional import Professional
from app.services.care_team_service import require_clinical_access


async def bind_conversation_patient(
    db: AsyncSession,
    professional: Professional,
    conversation: Conversation,
    patient_id: str | None,
) -> None:
    """If patient_id is set and conversation has no patient, assign after ownership check.

    If the conversation already has a different patient, keep the existing one.
    """
    if not patient_id:
        return
    access = await require_clinical_access(db, UUID(patient_id), professional)
    patient = access.patient
    if conversation.patient_id is None:
        conversation.patient_id = patient.id
