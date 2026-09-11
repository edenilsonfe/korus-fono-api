import json
from datetime import date
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import require_verified_professional
from app.core.utils import utcnow
from app.db.session import get_db
from app.models.ai import AIReport, AIReportRevision, ChatMessage, Conversation
from app.models.professional import Professional
from app.schemas.ai import (
    AICapabilitiesResponse,
    AIJobResponse,
    AIReportCreate,
    AIReportResponse,
    AIReportRevisionResponse,
    AIReportUpdate,
    AIToolRequest,
    ConversationCreate,
    ConversationResponse,
    ConversationUpdate,
    MessageCreate,
)
from app.schemas.assistant import ChatResponse
from app.schemas.report_delivery import ReportDeliveryCreate, ReportDeliveryResponse
from app.services import report_delivery_service
from app.services.ai_context import build_context
from app.services.ai_prompts import (
    AI_TOOL_SPECS,
    build_request_prompt,
    build_tool_prompt,
)
from app.services.ai_service import (
    build_patient_context,
    create_ai_job,
    get_job,
    run_llm,
)
from app.services.assistant.assistant_service import AssistantService
from app.services.assistant.conversation_patient import bind_conversation_patient
from app.services.assistant.rate_limit import enforce_assistant_rate_limit
from app.services.audio_transcription_service import transcribe_audio
from app.services.care_team_service import record_access_event, require_clinical_access
from app.services.report_export import export_report
from app.services.professional_branding import build_document_identity
from app.services.report_service import revise_report
from app.services.timeline import create_timeline_event

router = APIRouter(prefix="/ai", tags=["ai"])


async def _get_ai_patient(
    db: AsyncSession, patient_id: UUID, professional: Professional
):
    access = await require_clinical_access(db, patient_id, professional)
    record_access_event(
        db,
        patient_id=patient_id,
        actor=professional,
        actor_role=access.role,
        action="ai_context_used",
        resource_type="patient",
        resource_id=patient_id,
    )
    return access.patient


@router.get("/capabilities", response_model=AICapabilitiesResponse)
async def get_ai_capabilities(
    _professional: Professional = Depends(require_verified_professional),
):
    settings = get_settings()
    return AICapabilitiesResponse(
        llm_enabled=bool(settings.opencode_api_key.strip()),
        audio_transcription_enabled=bool(settings.audio_transcription_api_key.strip()),
    )


def _conversation_response(
    conv: Conversation, messages: list[ChatMessage] | None = None
) -> ConversationResponse:
    return ConversationResponse(
        id=str(conv.id),
        title=conv.title,
        patient_id=str(conv.patient_id) if conv.patient_id else None,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        messages=[
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "createdAt": m.created_at.isoformat(),
            }
            for m in (conv.messages if messages is None else messages)
        ],
    )


async def _get_owned_conversation(
    db: AsyncSession,
    conversation_id: UUID,
    professional: Professional,
) -> Conversation:
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.professional_id == professional.id,
        )
        .options(selectinload(Conversation.messages))
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    return conv


@router.get("/jobs/{job_id}", response_model=AIJobResponse)
async def poll_job(
    job_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    job = await get_job(db, job_id, professional.id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return AIJobResponse(
        id=str(job.id),
        job_type=job.job_type,
        status=job.status,
        result=job.result,
        error=job.error,
    )


@router.get("/reports", response_model=list[AIReportResponse])
async def list_reports(
    patient_id: UUID | None = Query(None, alias="patientId"),
    report_type: str | None = Query(None, alias="type"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    include_content: bool = Query(False, alias="includeContent"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    from app.models.patient import Patient

    query = (
        select(AIReport, Patient.name)
        .join(Patient, AIReport.patient_id == Patient.id)
        .where(AIReport.professional_id == professional.id)
    )
    if patient_id:
        query = query.where(AIReport.patient_id == patient_id)
    if report_type:
        query = query.where(AIReport.type == report_type)
    query = query.order_by(AIReport.date.desc(), AIReport.id.desc()).offset(offset).limit(limit)
    if not include_content:
        query = query.options(defer(AIReport.content))
    result = await db.execute(query)
    return [
        AIReportResponse(
            id=str(r.id),
            type=r.type,
            patient_id=str(r.patient_id),
            patient=name,
            date=r.date.isoformat(),
            preview=r.preview,
            content=r.content if include_content else "",
            status=r.status,
        )
        for r, name in result.all()
    ]


@router.patch("/reports/{report_id}", response_model=AIReportResponse)
async def update_report(
    report_id: UUID,
    body: AIReportUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    from app.models.patient import Patient

    await revise_report(db, report_id, professional.id, body)

    result = await db.execute(
        select(AIReport, Patient.name)
        .join(Patient, AIReport.patient_id == Patient.id)
        .where(AIReport.id == report_id, AIReport.professional_id == professional.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    report, patient_name = row
    await db.flush()
    return AIReportResponse(
        id=str(report.id),
        type=report.type,
        patient_id=str(report.patient_id),
        patient=patient_name,
        date=report.date.isoformat(),
        preview=report.preview,
        content=report.content,
        status=report.status,
    )


@router.get("/reports/{report_id}", response_model=AIReportResponse)
async def get_report(
    report_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    from app.models.patient import Patient
    row = (await db.execute(select(AIReport, Patient.name).join(Patient, AIReport.patient_id == Patient.id)
        .where(AIReport.id == report_id, AIReport.professional_id == professional.id))).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    report, patient_name = row
    return AIReportResponse(id=str(report.id), type=report.type, patient_id=str(report.patient_id),
        patient=patient_name, date=report.date.isoformat(), preview=report.preview,
        content=report.content, status=report.status)


@router.get("/reports/{report_id}/revisions", response_model=list[AIReportRevisionResponse])
async def get_report_revisions(
    report_id: UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await get_report(report_id, professional, db)
    rows = (await db.scalars(select(AIReportRevision).where(AIReportRevision.report_id == report_id)
        .order_by(AIReportRevision.created_at.desc(), AIReportRevision.id.desc()).offset(offset).limit(limit))).all()
    return [AIReportRevisionResponse(id=str(row.id), content=row.content, status=row.status,
        professional_id=str(row.professional_id), created_at=row.created_at) for row in rows]


@router.get("/reports/{report_id}/export")
async def export_report_file(
    report_id: UUID,
    format: str = Query(..., pattern="^(pdf|docx|txt|md)$"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    from app.models.patient import Patient

    result = await db.execute(
        select(AIReport, Patient.name)
        .join(Patient, AIReport.patient_id == Patient.id)
        .where(AIReport.id == report_id, AIReport.professional_id == professional.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    report, patient_name = row
    identity = await build_document_identity(professional)
    try:
        data, media_type, suffix = export_report(
            format, report.type, patient_name, report.date, report.content, identity=identity
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"relatorio-{report.type}-{report.date.isoformat()}.{suffix}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _delivery_response(delivery, *, url: str | None = None) -> ReportDeliveryResponse:
    return ReportDeliveryResponse(
        id=str(delivery.id),
        report_id=str(delivery.report_id),
        channel=delivery.channel,
        recipient_label=delivery.recipient_label,
        status=delivery.delivery_status,
        url=url,
        expires_at=delivery.expires_at,
        revoked_at=delivery.revoked_at,
        view_count=delivery.view_count or 0,
        download_count=delivery.download_count or 0,
        first_viewed_at=delivery.first_viewed_at,
        last_viewed_at=delivery.last_viewed_at,
        last_downloaded_at=delivery.last_downloaded_at,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
    )


async def _get_owned_report(
    db: AsyncSession, report_id: UUID, professional_id: UUID
) -> AIReport:
    report = await db.scalar(
        select(AIReport).where(
            AIReport.id == report_id,
            AIReport.professional_id == professional_id,
        )
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    return report


@router.post(
    "/reports/{report_id}/deliveries",
    response_model=ReportDeliveryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_report_delivery(
    report_id: UUID,
    body: ReportDeliveryCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Cria e envia a entrega (link/WhatsApp/e-mail) de um relatório finalizado."""
    report = await _get_owned_report(db, report_id, professional.id)
    delivery, token = await report_delivery_service.create_report_delivery(
        db, professional=professional, report=report, body=body
    )
    await db.commit()
    return _delivery_response(
        delivery, url=report_delivery_service.build_delivery_url(token)
    )


@router.get("/reports/{report_id}/deliveries", response_model=list[ReportDeliveryResponse])
async def list_report_deliveries(
    report_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_report(db, report_id, professional.id)
    rows = await report_delivery_service.list_report_deliveries(db, report_id=report_id)
    return [_delivery_response(row) for row in rows]


@router.delete(
    "/reports/{report_id}/deliveries/{delivery_id}",
    response_model=ReportDeliveryResponse,
)
async def revoke_report_delivery(
    report_id: UUID,
    delivery_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_report(db, report_id, professional.id)
    delivery = await report_delivery_service.revoke_report_delivery(
        db, report_id=report_id, delivery_id=delivery_id
    )
    await db.commit()
    return _delivery_response(delivery)


@router.post("/reports", response_model=AIReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    body: AIReportCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    patient = await _get_ai_patient(db, UUID(body.patient_id), professional)
    spec_key = f"report:{body.type}"
    if spec_key not in AI_TOOL_SPECS:
        raise HTTPException(status_code=400, detail="Tipo de relatório inválido")
    spec = AI_TOOL_SPECS[spec_key]
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=patient.id,
        job_type="report",
        input_data=body.model_dump(),
    )
    context = await build_context(db, patient.id, spec.sections, limits=spec.limits)
    prompt = build_tool_prompt(spec, context=context, extra_prompt=body.prompt)
    content = await run_llm(prompt, spec.system, output=spec.output)
    preview = content[:200] + "..." if len(content) > 200 else content
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type=body.type,
        date=date.today(),
        preview=preview,
        content=content,
        status="draft",
    )
    db.add(report)
    await db.flush()
    job.status = "completed"
    job.result = json.dumps({"reportId": str(report.id)})
    job.completed_at = utcnow()
    await db.flush()
    await create_timeline_event(
        db,
        patient_id=patient.id,
        professional_id=professional.id,
        event_type="relatorio",
        title=f"Relatório {body.type} gerado por IA",
        description=preview,
        source_id=report.id,
    )
    # ponytail: commit before response — get_db commits after send, so a follow-up GET can miss the row
    await db.commit()
    return AIReportResponse(
        id=str(report.id),
        type=report.type,
        patient_id=str(report.patient_id),
        patient=patient.name,
        date=report.date.isoformat(),
        preview=report.preview,
        content=report.content,
        status=report.status,
    )


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.professional_id == professional.id)
        .order_by(Conversation.updated_at.desc())
    )
    convs = result.scalars().all()
    return [_conversation_response(c, messages=[]) for c in convs]


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    conv = await _get_owned_conversation(db, conversation_id, professional)
    return _conversation_response(conv)


@router.post("/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    patient_id = UUID(body.patient_id) if body.patient_id else None
    if patient_id:
        await _get_ai_patient(db, patient_id, professional)
    conv = Conversation(
        professional_id=professional.id,
        patient_id=patient_id,
        title=body.title or "Nova conversa",
    )
    db.add(conv)
    await db.flush()
    # New row has no messages; mark collection loaded without async lazy-load.
    set_committed_value(conv, "messages", [])
    return _conversation_response(conv)


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: UUID,
    body: ConversationUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    conv = await _get_owned_conversation(db, conversation_id, professional)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Título não pode ser vazio")
    conv.title = title[:255]
    await db.flush()
    return _conversation_response(conv)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    conv = await _get_owned_conversation(db, conversation_id, professional)
    await db.delete(conv)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/messages", response_model=ChatResponse)
async def send_message(
    conversation_id: UUID,
    body: MessageCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Unified AI assistant (clínico + gestão) with tool-calling.

    Returns a single ChatResponse (JSON) after orchestrating read-only tools.
    Rate-limited per professional; 503 if OpenCode is not configured.
    """
    conv = await _get_owned_conversation(db, conversation_id, professional)

    await bind_conversation_patient(db, professional, conv, body.patient_id)

    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))

    user_msg = ChatMessage(conversation_id=conv.id, role="user", content=body.content)
    db.add(user_msg)
    await db.flush()
    # Make the new user message visible to the service's history view.
    conv.messages = list(conv.messages or []) + [user_msg]

    service = AssistantService(db, professional, conv)
    response = await service.chat(body.content)

    assistant_msg = ChatMessage(
        conversation_id=conv.id, role="assistant", content=response.reply
    )
    db.add(assistant_msg)
    await db.flush()

    return response


async def _run_tool_job(
    db: AsyncSession,
    professional: Professional,
    job_type: str,
    body: AIToolRequest,
    *,
    spec_key: str | None = None,
    prompt_builder=None,
) -> dict:
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    patient_id = UUID(body.patient_id) if body.patient_id else None
    if patient_id:
        await _get_ai_patient(db, patient_id, professional)
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=patient_id,
        job_type=job_type,
        input_data=body.model_dump(),
    )
    if spec_key:
        spec, prompt = build_request_prompt(
            spec_key,
            body,
            context=await build_context(db, patient_id, AI_TOOL_SPECS[spec_key].sections, limits=AI_TOOL_SPECS[spec_key].limits)
            if patient_id and AI_TOOL_SPECS[spec_key].sections
            else "",
        )
        result = await run_llm(prompt, spec.system, output=spec.output)
    else:
        context = await build_patient_context(db, patient_id) if patient_id else ""
        prompt = prompt_builder(body, context)
        result = await run_llm(prompt)
    job.status = "completed"
    job.result = result
    job.completed_at = utcnow()
    await db.flush()
    return {"jobId": str(job.id), "status": "completed", "result": result}


@router.post("/transcribe", status_code=status.HTTP_200_OK)
async def transcribe(
    patient_id: str = Form(alias="patientId"),
    file: UploadFile = File(...),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    parsed_patient_id = UUID(patient_id)
    await _get_ai_patient(db, parsed_patient_id, professional)
    transcription = await transcribe_audio(file)
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=parsed_patient_id,
        job_type="transcribe",
        input_data={
            "filename": transcription.filename,
            "contentType": transcription.content_type,
            "sizeBytes": transcription.size_bytes,
            "audioSha256": transcription.sha256,
        },
    )
    job.status = "completed"
    job.result = transcription.text
    job.completed_at = utcnow()
    await db.flush()
    return {"jobId": str(job.id), "status": "completed", "result": transcription.text}

@router.post("/speech-analysis", status_code=status.HTTP_200_OK)
async def speech_analysis(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(
        db,
        professional,
        "speech-analysis",
        body,
        prompt_builder=lambda b, c: (
            "Produza uma análise preliminar e conservadora do texto transcrito. "
            "Não invente características acústicas ou articulações ausentes e recomende revisão "
            f"fonoaudiológica.\nAmostra:\n{b.text or ''}\nContexto:\n{c}"
        ),
    )


@router.post("/speech-analysis/audio", status_code=status.HTTP_200_OK)
async def speech_analysis_audio(
    patient_id: str = Form(alias="patientId"),
    file: UploadFile = File(...),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await run_in_threadpool(enforce_assistant_rate_limit, str(professional.id))
    if not get_settings().opencode_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ferramentas de IA não configuradas.",
        )
    parsed_patient_id = UUID(patient_id)
    await _get_ai_patient(db, parsed_patient_id, professional)
    transcription = await transcribe_audio(file)
    context = await build_patient_context(db, parsed_patient_id)
    result = await run_llm(
        "Produza uma análise preliminar e conservadora da amostra transcrita abaixo. "
        "Não invente características acústicas, articulações ou fonemas que não estejam explícitos "
        "no texto. Informe as limitações da transcrição automática e recomende revisão "
        f"fonoaudiológica.\nAmostra transcrita:\n{transcription.text}\nContexto:\n{context}"
    )
    job = await create_ai_job(
        db,
        professional_id=professional.id,
        patient_id=parsed_patient_id,
        job_type="speech-analysis",
        input_data={
            "filename": transcription.filename,
            "contentType": transcription.content_type,
            "sizeBytes": transcription.size_bytes,
            "audioSha256": transcription.sha256,
        },
    )
    job.status = "completed"
    job.result = result
    job.completed_at = utcnow()
    await db.flush()
    return {"jobId": str(job.id), "status": "completed", "result": result}

@router.post("/clinical-trends", status_code=status.HTTP_200_OK)
async def clinical_trends(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(db, professional, "clinical-trends", body, spec_key="clinical-trends")

@router.post("/suggest-goals", status_code=status.HTTP_200_OK)
async def suggest_goals(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(db, professional, "suggest-goals", body, spec_key="suggest-goals")

@router.post("/therapy-plan", status_code=status.HTTP_200_OK)
async def therapy_plan(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(db, professional, "therapy-plan", body, spec_key="therapy-plan")

@router.post("/session-summary", status_code=status.HTTP_200_OK)
async def session_summary(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(db, professional, "session-summary", body, spec_key="session-summary")

@router.post("/proofread", status_code=status.HTTP_200_OK)
async def proofread(body: AIToolRequest, professional: Professional = Depends(require_verified_professional), db: AsyncSession = Depends(get_db)):
    return await _run_tool_job(db, professional, "proofread", body, spec_key="proofread")
