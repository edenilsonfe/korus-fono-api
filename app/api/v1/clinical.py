from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_verified_professional
from app.core.utils import goal_status_from_progress
from app.db.session import get_db
from app.models.assessment import (
    ASSESSMENT_STATUS_COMPLETED,
    Assessment,
    ProtocolCatalog,
)
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.schemas.clinical import (
    AssessmentCancelResponse,
    AssessmentComparisonMetricResponse,
    AssessmentComparisonResponse,
    AssessmentComparisonSideResponse,
    AssessmentCreate,
    AssessmentDraftUpsert,
    AssessmentFinalize,
    AssessmentsPage,
    AssessmentStatusCounts,
    AssessmentChangedItemResponse,
    GoalCreate,
    GoalUpdate,
    ProtocolResponse,
)
from app.schemas.patient import (
    AssessmentResponse,
    DevelopmentAnalyticsAreaResponse,
    GoalResponse,
)
from app.services.assessment_scoring import get_protocol_scoring_mode
from app.services.assessment_comparison import compare_assessments
from app.services.assessment_service import (
    complete_assessment_draft,
    create_assessment_record,
    get_assessment_draft,
    upsert_assessment_draft,
)
from app.services.care_team_service import require_clinical_access
from app.services.patient import build_clinical_domains, build_development_analytics
from app.services.patient_access import list_accessible_patient_ids
from app.services.timeline import create_timeline_event

ANALYTICS_PERIODS = frozenset({"30d", "90d", "6m", "1y"})


def _assessment_response(
    assessment: Assessment,
    protocol_name: str,
    professional_name: str,
    *,
    patient: Patient | None = None,
) -> AssessmentResponse:
    return AssessmentResponse(
        id=str(assessment.id),
        protocol=protocol_name,
        protocol_id=assessment.protocol_id,
        date=assessment.date.isoformat(),
        professional=professional_name,
        result=assessment.result,
        percentage=assessment.percentage,
        interpretation=assessment.interpretation,
        fields=assessment.fields or [],
        patient_id=str(patient.id) if patient else None,
        patient_name=patient.name if patient else None,
        avatar_color=patient.avatar_color if patient else None,
        answers=assessment.answers or {},
        scores=assessment.scores,
        status=assessment.status,
        informant=assessment.informant,
        metadata=assessment.assessment_metadata,
    )


router = APIRouter(tags=["clinical"])


@router.get("/protocols", response_model=list[ProtocolResponse])
async def list_protocols(
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProtocolCatalog)
        .where(ProtocolCatalog.is_active.is_(True))
        .order_by(ProtocolCatalog.sort_order.asc(), ProtocolCatalog.name.asc())
    )
    protocols = result.scalars().all()
    patient_ids = await list_accessible_patient_ids(db, professional)
    stats_rows = await db.execute(
        select(Assessment.protocol_id, func.count(), func.avg(Assessment.percentage), func.max(Assessment.date))
        .join(Patient, Assessment.patient_id == Patient.id)
        .where(Patient.id.in_(patient_ids), Assessment.status == ASSESSMENT_STATUS_COMPLETED)
        .group_by(Assessment.protocol_id)
    )
    stats_by_protocol = {row[0]: tuple(row[1:]) for row in stats_rows}
    responses = []
    for p in protocols:
        count, avg_result, last_applied = stats_by_protocol.get(p.id, (0, 0, None))
        responses.append(
            ProtocolResponse(
                id=p.id,
                name=p.name,
                full_name=p.full_name,
                description=p.description,
                age_range=p.age_range,
                fields=[{"key": f.get("key", f["label"].lower().replace(" ", "_")), "label": f["label"]} for f in (p.field_templates or [])],
                applications=count or 0,
                avg_result=float(avg_result or 0),
                last_applied=last_applied.isoformat() if last_applied else None,
                scoring_mode=get_protocol_scoring_mode(p.id),
            )
        )
    return responses


@router.get("/protocols/{protocol_id}", response_model=ProtocolResponse)
async def get_protocol(protocol_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ProtocolCatalog).where(
            ProtocolCatalog.id == protocol_id,
            ProtocolCatalog.is_active.is_(True),
        )
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Protocolo não encontrado")
    return ProtocolResponse(
        id=p.id,
        name=p.name,
        full_name=p.full_name,
        description=p.description,
        age_range=p.age_range,
        fields=[{"key": f.get("key", f["label"].lower().replace(" ", "_")), "label": f["label"]} for f in (p.field_templates or [])],
        scoring_mode=get_protocol_scoring_mode(p.id),
    )


def _assessment_period_start(period: str | None) -> date | None:
    if not period or period == "all":
        return None
    today = date.today()
    normalized = period.strip().lower()
    if normalized == "week":
        # Segunda-feira da semana corrente (ISO).
        return today - timedelta(days=today.weekday())
    if normalized == "month":
        return today.replace(day=1)
    return None


@router.get("/assessments", response_model=AssessmentsPage)
async def list_assessments_global(
    protocol: str | None = None,
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Filtro: draft | completed | cancelled | awaiting_informant",
    ),
    period: str | None = Query(
        None,
        description="Filtro temporal: week | month | all",
    ),
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if period and period.strip().lower() not in {"week", "month", "all"}:
        raise HTTPException(status_code=400, detail="Período inválido. Use: week, month, all")

    awaiting_clause = Assessment.result.ilike("%aguardando%")
    patient_ids = await list_accessible_patient_ids(db, professional)
    scope = [Patient.id.in_(patient_ids)]
    if protocol:
        scope.append(Assessment.protocol_id == protocol.lower())
    period_start = _assessment_period_start(period)
    if period_start is not None:
        scope.append(Assessment.date >= period_start)
    if q:
        scope.append(Patient.name.ilike(f"%{q}%"))

    counts_row = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(Assessment.status == "draft", ~awaiting_clause), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (and_(Assessment.status == "draft", awaiting_clause), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Assessment.status == "completed", 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Assessment.status == "cancelled", 1), else_=0)),
                    0,
                ),
            )
            .select_from(Assessment)
            .join(Patient, Assessment.patient_id == Patient.id)
            .where(*scope)
        )
    ).one()
    status_counts = AssessmentStatusCounts(
        all=int(counts_row[0] or 0),
        draft=int(counts_row[1] or 0),
        awaiting_informant=int(counts_row[2] or 0),
        completed=int(counts_row[3] or 0),
        cancelled=int(counts_row[4] or 0),
    )

    query = (
        select(Assessment, Patient, ProtocolCatalog, Professional)
        .join(Patient, Assessment.patient_id == Patient.id)
        .join(ProtocolCatalog, Assessment.protocol_id == ProtocolCatalog.id)
        .join(Professional, Professional.id == Assessment.professional_id)
        .where(*scope)
    )
    if status_filter:
        normalized = status_filter.strip().lower()
        allowed = {"draft", "completed", "cancelled", "awaiting_informant"}
        if normalized not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Status inválido. Use: {', '.join(sorted(allowed))}",
            )
        if normalized == "awaiting_informant":
            query = query.where(Assessment.status == "draft", awaiting_clause)
        elif normalized == "draft":
            query = query.where(Assessment.status == "draft", ~awaiting_clause)
        else:
            query = query.where(Assessment.status == normalized)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(Assessment.date.desc()).offset((page - 1) * limit).limit(limit)
    )
    items = [
        _assessment_response(a, proto.name, author.name, patient=patient)
        for a, patient, proto, author in result.all()
    ]
    return AssessmentsPage(
        items=items,
        total=total or 0,
        page=page,
        limit=limit,
        status_counts=status_counts,
    )


@router.post(
    "/assessments/{assessment_id}/cancel",
    response_model=AssessmentCancelResponse,
)
async def cancel_assessment(
    assessment_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Assessment)
        .where(
            Assessment.id == assessment_id,
            Assessment.professional_id == professional.id,
        )
    )
    assessment = result.scalar_one_or_none()
    if assessment is None:
        raise HTTPException(status_code=404, detail="Avaliação não encontrada")
    await require_clinical_access(db, assessment.patient_id, professional, "clinical:write")
    if assessment.status != "draft":
        raise HTTPException(
            status_code=400,
            detail="Só é possível cancelar avaliações em rascunho",
        )
    assessment.status = "cancelled"
    if not assessment.result or assessment.result.lower() in {
        "rascunho",
        "aguardando informante",
        "em coordenação",
        "em coordenação",
    }:
        assessment.result = "Cancelada"
    await db.commit()
    return AssessmentCancelResponse(id=str(assessment.id), status=assessment.status)


patient_router = APIRouter(prefix="/patients/{patient_id}", tags=["clinical"])


@patient_router.get("/assessments", response_model=list[AssessmentResponse])
async def list_patient_assessments(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional)
    result = await db.execute(
        select(Assessment, ProtocolCatalog, Professional)
        .join(ProtocolCatalog, Assessment.protocol_id == ProtocolCatalog.id)
        .join(Professional, Professional.id == Assessment.professional_id)
        .where(Assessment.patient_id == patient_id)
        .order_by(Assessment.date.desc())
    )
    return [
        _assessment_response(a, p.name, author.name)
        for a, p, author in result.all()
    ]


@patient_router.get("/assessments/compare", response_model=AssessmentComparisonResponse)
async def compare_patient_assessments(
    patient_id: UUID,
    base_id: UUID = Query(..., alias="baseId"),
    target_id: UUID = Query(..., alias="targetId"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    """Delta entre duas aplicações concluídas do mesmo protocolo (reavaliação)."""
    await require_clinical_access(db, patient_id, professional)
    result = await compare_assessments(
        db,
        patient_id=patient_id,
        base_id=base_id,
        target_id=target_id,
    )
    return AssessmentComparisonResponse(
        protocol_id=result.protocol_id,
        protocol_name=result.protocol_name,
        base=AssessmentComparisonSideResponse(
            id=result.base.id,
            date=result.base.date,
            result=result.base.result,
            percentage=result.base.percentage,
        ),
        target=AssessmentComparisonSideResponse(
            id=result.target.id,
            date=result.target.date,
            result=result.target.result,
            percentage=result.target.percentage,
        ),
        percentage_delta=result.percentage_delta,
        metrics=[
            AssessmentComparisonMetricResponse(
                key=metric.key,
                label=metric.label,
                base=metric.base,
                target=metric.target,
                delta=metric.delta,
            )
            for metric in result.metrics
        ],
        answers_changed=result.answers_changed,
        answers_total=result.answers_total,
        changed_items=[
            AssessmentChangedItemResponse(
                key=item.key, base=item.base, target=item.target
            )
            for item in result.changed_items
        ],
        summary=result.summary,
    )


@patient_router.get(
    "/assessments/drafts/{protocol_id}",
    response_model=AssessmentResponse | None,
)
async def get_patient_assessment_draft(
    patient_id: UUID,
    protocol_id: str,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional)
    assessment = await get_assessment_draft(
        db, patient_id, protocol_id, professional.id
    )
    if assessment is None:
        return None
    protocol = await db.get(ProtocolCatalog, assessment.protocol_id)
    if protocol is None:
        raise HTTPException(status_code=404, detail="Protocolo não encontrado")
    return _assessment_response(assessment, protocol.name, professional.name)


@patient_router.put(
    "/assessments/drafts/{protocol_id}",
    response_model=AssessmentResponse,
)
async def save_patient_assessment_draft(
    patient_id: UUID,
    protocol_id: str,
    body: AssessmentDraftUpsert,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    access = await require_clinical_access(db, patient_id, professional, "clinical:write")
    patient = access.patient
    assessment, protocol = await upsert_assessment_draft(
        db,
        patient,
        professional,
        protocol_id,
        body,
    )
    return _assessment_response(assessment, protocol.name, professional.name)


@patient_router.post(
    "/assessments/drafts/{protocol_id}/complete",
    response_model=AssessmentResponse,
)
async def finalize_patient_assessment_draft(
    patient_id: UUID,
    protocol_id: str,
    body: AssessmentFinalize,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    access = await require_clinical_access(db, patient_id, professional, "clinical:write")
    patient = access.patient
    assessment, protocol = await complete_assessment_draft(
        db,
        patient,
        professional,
        protocol_id,
        body,
    )
    return _assessment_response(assessment, protocol.name, professional.name)


@patient_router.post("/assessments", response_model=AssessmentResponse, status_code=status.HTTP_201_CREATED)
async def create_assessment(
    patient_id: UUID,
    body: AssessmentCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    access = await require_clinical_access(db, patient_id, professional, "clinical:write")
    patient = access.patient
    assessment, protocol = await create_assessment_record(
        db,
        patient,
        professional,
        body,
    )
    return _assessment_response(assessment, protocol.name, professional.name)


@patient_router.get("/goals", response_model=list[GoalResponse])
async def list_goals(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional)
    result = await db.execute(
        select(Goal, Professional)
        .join(Professional, Professional.id == Goal.professional_id)
        .where(Goal.patient_id == patient_id)
    )
    return [
        GoalResponse(
            id=str(g.id),
            title=g.title,
            progress=g.progress,
            area=g.area,
            professional=author.name,
            start_date=g.start_date.isoformat(),
            status=g.status,
        )
        for g, author in result.all()
    ]


@patient_router.post("/goals", response_model=GoalResponse, status_code=status.HTTP_201_CREATED)
async def create_goal(
    patient_id: UUID,
    body: GoalCreate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional, "clinical:write")
    progress = body.progress
    status_val = body.status or goal_status_from_progress(progress)
    goal = Goal(
        patient_id=patient_id,
        professional_id=professional.id,
        title=body.title,
        area=body.area,
        progress=progress,
        start_date=date.fromisoformat(body.start_date) if body.start_date else date.today(),
        status=status_val,
    )
    db.add(goal)
    await db.flush()
    return GoalResponse(
        id=str(goal.id),
        title=goal.title,
        progress=goal.progress,
        area=goal.area,
        professional=professional.name,
        start_date=goal.start_date.isoformat(),
        status=goal.status,
    )


@patient_router.patch("/goals/{goal_id}", response_model=GoalResponse)
async def update_goal(
    patient_id: UUID,
    goal_id: UUID,
    body: GoalUpdate,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional, "clinical:write")
    result = await db.execute(
        select(Goal).where(
            Goal.id == goal_id,
            Goal.patient_id == patient_id,
            Goal.professional_id == professional.id,
        )
    )
    goal = result.scalar_one_or_none()
    if not goal:
        raise HTTPException(status_code=404, detail="Meta não encontrada")
    data = body.model_dump(exclude_unset=True)
    if "progress" in data and "status" not in data:
        data["status"] = goal_status_from_progress(data["progress"])
    for field, value in data.items():
        setattr(goal, field, value)
    await db.flush()
    if goal.progress >= 75:
        await create_timeline_event(
            db,
            patient_id=patient_id,
            professional_id=professional.id,
            event_type="meta",
            title="Meta atingida",
            description=goal.title,
            source_id=goal.id,
        )
    return GoalResponse(
        id=str(goal.id),
        title=goal.title,
        progress=goal.progress,
        area=goal.area,
        professional=professional.name,
        start_date=goal.start_date.isoformat(),
        status=goal.status,
    )


@patient_router.get("/clinical-domains")
async def get_clinical_domains(
    patient_id: UUID,
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    await require_clinical_access(db, patient_id, professional)
    return await build_clinical_domains(db, patient_id)


@router.get("/analytics/development")
async def analytics_development(
    patient_id: UUID | None = Query(None),
    period: str = Query("6m"),
    professional: Professional = Depends(require_verified_professional),
    db: AsyncSession = Depends(get_db),
):
    if period not in ANALYTICS_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"period must be one of: {', '.join(sorted(ANALYTICS_PERIODS))}",
        )
    if not patient_id:
        return {"areas": []}
    await require_clinical_access(db, patient_id, professional)
    domains = await build_development_analytics(db, patient_id, period)
    return {
        "areas": [DevelopmentAnalyticsAreaResponse.model_validate(d) for d in domains]
    }
