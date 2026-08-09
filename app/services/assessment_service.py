from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Assessment, ProtocolCatalog
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.clinical import AssessmentCreate, AssessmentDraftUpsert, AssessmentFinalize
from app.services.assessment_scoring import get_protocol_scoring_mode
from app.services.clinical_activity import record_assessment
from app.services.mchat_validation import validate_mchat_submission
from app.services.scoring_session import ScoreError, ScoringSession


async def _active_protocol(db: AsyncSession, protocol_id: str) -> ProtocolCatalog:
    protocol = await db.get(ProtocolCatalog, protocol_id.lower())
    if protocol is None or not protocol.is_active:
        raise HTTPException(status_code=404, detail="Protocolo não encontrado")
    return protocol


def _prepare_values(protocol: ProtocolCatalog, body: AssessmentCreate) -> dict:
    answers = body.answers or {}
    scores = body.scores
    result_text = body.result
    percentage = body.percentage
    interpretation = body.interpretation
    fields = [field.model_dump() for field in body.fields]

    mode = get_protocol_scoring_mode(protocol.id)
    if answers and mode == "manifest" and scores is None:
        try:
            normalized = ScoringSession.from_protocol(protocol.id, "manifest").score(answers)
        except ScoreError as exc:
            detail = str(exc)
            code = (
                404
                if "não encontrado" in detail.lower() or "não possui pacote" in detail.lower()
                else 400
            )
            raise HTTPException(status_code=code, detail=detail) from exc
        scores = normalized.raw_scores
        result_text = result_text or normalized.result
        percentage = percentage or normalized.percentage
        interpretation = interpretation or normalized.interpretation
        fields = fields or normalized.to_assessment_fields()
    elif scores:
        normalized = ScoringSession.from_scores(scores).score({})
        result_text = result_text or normalized.result
        percentage = percentage or normalized.percentage
        interpretation = interpretation or normalized.interpretation
        fields = fields or normalized.to_assessment_fields()

    if not result_text:
        raise HTTPException(status_code=400, detail="Resultado da avaliação é obrigatório")
    return {
        "date": date.fromisoformat(body.date) if body.date else date.today(),
        "result": result_text,
        "percentage": percentage,
        "interpretation": interpretation,
        "fields": fields,
        "answers": answers,
        "scores": scores,
        "status": body.status,
        "informant": body.informant,
        "assessment_metadata": body.metadata,
    }


async def create_assessment_record(
    db: AsyncSession,
    patient: Patient,
    professional: Professional,
    body: AssessmentCreate,
) -> tuple[Assessment, ProtocolCatalog]:
    protocol = await _active_protocol(db, body.protocol_id)
    validate_mchat_submission(patient, protocol.id, body)
    assessment = Assessment(
        patient_id=patient.id,
        professional_id=professional.id,
        protocol_id=protocol.id,
        **_prepare_values(protocol, body),
    )
    db.add(assessment)
    await db.flush()
    if assessment.status == "completed":
        await record_assessment(
            db,
            assessment=assessment,
            protocol_name=protocol.name,
            professional=professional,
        )
    return assessment, protocol


async def get_assessment_draft(
    db: AsyncSession,
    patient_id,
    protocol_id: str,
) -> Assessment | None:
    return await db.scalar(
        select(Assessment)
        .where(
            Assessment.patient_id == patient_id,
            Assessment.protocol_id == protocol_id.lower(),
            Assessment.status == "draft",
        )
        .order_by(Assessment.updated_at.desc(), Assessment.created_at.desc())
        .limit(1)
    )


async def _lock_patient(db: AsyncSession, patient_id) -> None:
    await db.execute(
        select(Patient.id).where(Patient.id == patient_id).with_for_update()
    )


async def upsert_assessment_draft(
    db: AsyncSession,
    patient: Patient,
    professional: Professional,
    protocol_id: str,
    body: AssessmentDraftUpsert,
) -> tuple[Assessment, ProtocolCatalog]:
    protocol = await _active_protocol(db, protocol_id)
    # Serializa autosaves concorrentes para manter um único rascunho ativo.
    await _lock_patient(db, patient.id)
    assessment = await get_assessment_draft(db, patient.id, protocol.id)
    if assessment is None:
        assessment = Assessment(
            patient_id=patient.id,
            professional_id=professional.id,
            protocol_id=protocol.id,
            date=date.today(),
            result="Rascunho",
            percentage=0,
            interpretation="",
            fields=[],
            answers=body.answers or {},
            scores=None,
            status="draft",
            informant=body.informant,
            assessment_metadata=body.metadata,
        )
        db.add(assessment)
    else:
        assessment.answers = body.answers or {}
        assessment.informant = body.informant
        assessment.assessment_metadata = body.metadata
    await db.flush()
    return assessment, protocol


async def complete_assessment_draft(
    db: AsyncSession,
    patient: Patient,
    professional: Professional,
    protocol_id: str,
    body: AssessmentFinalize,
) -> tuple[Assessment, ProtocolCatalog]:
    create_body = AssessmentCreate(
        protocol_id=protocol_id,
        status="completed",
        **body.model_dump(),
    )
    protocol = await _active_protocol(db, protocol_id)
    validate_mchat_submission(patient, protocol.id, create_body)
    values = _prepare_values(protocol, create_body)
    await _lock_patient(db, patient.id)
    assessment = await get_assessment_draft(db, patient.id, protocol.id)
    if assessment is None:
        assessment = Assessment(
            patient_id=patient.id,
            professional_id=professional.id,
            protocol_id=protocol.id,
            **values,
        )
        db.add(assessment)
    else:
        for field, value in values.items():
            setattr(assessment, field, value)
    await db.flush()
    await record_assessment(
        db,
        assessment=assessment,
        protocol_name=protocol.name,
        professional=professional,
    )
    return assessment, protocol
