from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.patient import Patient
from app.models.session import Session
from app.models.ai import AIReport


def is_appointment_past(appt_date: date, appt_time: time, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    appt_dt = datetime.combine(appt_date, appt_time)
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    return appt_dt < now_naive


def derive_agenda_status(
    appt_status: str,
    appt_date: date,
    appt_time: time,
    has_session_on_date: bool,
    now: datetime | None = None,
) -> str:
    """Deriva status exibido na agenda de hoje.

    ponytail: badge de pagamento atrasado omitido — sem modelo de pagamento por paciente.
    """
    if appt_status not in ("pendente", "confirmado"):
        return appt_status
    if is_appointment_past(appt_date, appt_time, now) and not has_session_on_date:
        return "evolucao_pendente"
    return appt_status


def build_suggestions(pending: dict) -> list[dict]:
    suggestions: list[dict] = []
    evolutions = pending.get("evolutions", 0)
    reports = pending.get("reports", 0)
    sessions = pending.get("sessions", 0)
    assessment_drafts = pending.get("assessmentDrafts", 0) or pending.get(
        "assessment_drafts", 0
    )
    awaiting_informant = pending.get("awaitingInformant", 0) or pending.get(
        "awaiting_informant", 0
    )

    if evolutions > 0:
        suggestions.append({
            "id": "pending-evolutions",
            "title": "Evoluções pendentes",
            "text": (
                f"Você tem {evolutions} evolução pendente de registrar."
                if evolutions == 1
                else f"Você tem {evolutions} evoluções pendentes de registrar."
            ),
            "ctaLabel": "Ver na agenda",
            "ctaTo": "/agenda",
        })
    if assessment_drafts > 0:
        suggestions.append({
            "id": "pending-assessment-drafts",
            "title": "Avaliações em rascunho",
            "text": (
                f"Você tem {assessment_drafts} avaliação em rascunho para continuar."
                if assessment_drafts == 1
                else f"Você tem {assessment_drafts} avaliações em rascunho para continuar."
            ),
            "ctaLabel": "Ver rascunhos",
            "ctaTo": "/avaliacoes?status=draft",
        })
    if awaiting_informant > 0:
        suggestions.append({
            "id": "pending-awaiting-informant",
            "title": "Aguardando informante",
            "text": (
                f"Você tem {awaiting_informant} avaliação aguardando resposta do informante."
                if awaiting_informant == 1
                else f"Você tem {awaiting_informant} avaliações aguardando resposta do informante."
            ),
            "ctaLabel": "Ver pendências",
            "ctaTo": "/avaliacoes?status=awaiting_informant",
        })
    if reports > 0:
        suggestions.append({
            "id": "pending-reports",
            "title": "Relatórios em rascunho",
            "text": (
                f"Você tem {reports} relatório em rascunho aguardando finalização."
                if reports == 1
                else f"Você tem {reports} relatórios em rascunho aguardando finalização."
            ),
            "ctaLabel": "Ver relatórios",
            "ctaTo": "/relatorios",
        })
    if sessions > 0:
        suggestions.append({
            "id": "pending-sessions",
            "title": "Sessões agendadas",
            "text": (
                f"Você tem {sessions} sessão agendada nos próximos dias."
                if sessions == 1
                else f"Você tem {sessions} sessões agendadas nos próximos dias."
            ),
            "ctaLabel": "Ver agenda",
            "ctaTo": "/agenda",
        })
    return suggestions


async def _patients_with_session_on_dates(
    db: AsyncSession,
    professional_id,
    pairs: set[tuple],
) -> set[tuple]:
    if not pairs:
        return set()
    patient_ids = {p for p, _ in pairs}
    dates = {d for _, d in pairs}
    result = await db.execute(
        select(Session.patient_id, func.date(Session.date))
        .where(
            Session.professional_id == professional_id,
            Session.patient_id.in_(patient_ids),
            func.date(Session.date).in_(dates),
        )
    )
    # func.date() devolve str no SQLite (testes) e date no Postgres — normalizar
    return {
        (row[0], row[1] if isinstance(row[1], date) else date.fromisoformat(str(row[1])))
        for row in result.all()
    }


async def build_dashboard(db: AsyncSession, professional_id) -> dict:
    today = date.today()
    now = datetime.now()
    month_start = today.replace(day=1)

    active_patients = await db.scalar(
        select(func.count()).select_from(Patient).where(
            Patient.professional_id == professional_id, Patient.status == "ativo"
        )
    )
    new_this_month = await db.scalar(
        select(func.count()).select_from(Patient).where(
            Patient.professional_id == professional_id, Patient.start_date >= month_start
        )
    )
    sessions_done = await db.scalar(
        select(func.count())
        .select_from(Session)
        .join(Patient, Session.patient_id == Patient.id)
        .where(Patient.professional_id == professional_id)
    )
    sessions_pending = await db.scalar(
        select(func.count()).select_from(Appointment).where(
            Appointment.professional_id == professional_id,
            Appointment.status.in_(["pendente", "confirmado"]),
            Appointment.date >= today,
        )
    )
    ai_reports = await db.scalar(
        select(func.count()).select_from(AIReport).where(AIReport.professional_id == professional_id)
    )
    reports_draft = await db.scalar(
        select(func.count()).select_from(AIReport).where(
            AIReport.professional_id == professional_id,
            AIReport.status == "draft",
        )
    )

    # Monthly growth - last 6 months
    monthly_growth = []
    for i in range(5, -1, -1):
        ref = today.replace(day=1) - timedelta(days=i * 30)
        month_start_i = ref.replace(day=1)
        if month_start_i.month == 12:
            month_end = month_start_i.replace(year=month_start_i.year + 1, month=1)
        else:
            month_end = month_start_i.replace(month=month_start_i.month + 1)
        count = await db.scalar(
            select(func.count()).select_from(Patient).where(
                Patient.professional_id == professional_id,
                Patient.start_date >= month_start_i,
                Patient.start_date < month_end,
            )
        )
        month_names = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
        monthly_growth.append({"month": month_names[month_start_i.month - 1], "pacientes": count or 0})

    # Protocols applied
    protocols_result = await db.execute(
        select(Assessment.protocol_id, func.count())
        .join(Patient, Assessment.patient_id == Patient.id)
        .where(Patient.professional_id == professional_id)
        .group_by(Assessment.protocol_id)
        .order_by(func.count().desc())
        .limit(10)
    )
    protocols_applied = [{"name": row[0].upper(), "value": row[1]} for row in protocols_result.all()]

    # Today's agenda (all statuses)
    today_appts_result = await db.execute(
        select(Appointment, Patient)
        .join(Patient, Appointment.patient_id == Patient.id)
        .where(
            Appointment.professional_id == professional_id,
            Appointment.date == today,
        )
        .order_by(Appointment.time.asc())
    )
    today_rows = today_appts_result.all()
    today_pairs = {(appt.patient_id, today) for appt, _ in today_rows}
    sessions_today = await _patients_with_session_on_dates(db, professional_id, today_pairs)

    today_agenda = []
    upcoming = []
    for appt, patient in today_rows:
        has_session = (appt.patient_id, today) in sessions_today
        status = derive_agenda_status(appt.status, appt.date, appt.time, has_session, now)
        item = {
            "id": str(appt.id),
            "time": appt.time.strftime("%H:%M"),
            "patientId": str(appt.patient_id),
            "patientName": patient.name,
            "type": appt.type,
            "status": status,
        }
        today_agenda.append(item)
        if appt.status in ("pendente", "confirmado"):
            upcoming.append({
                "id": str(appt.id),
                "time": appt.time.strftime("%H:%M"),
                "patientName": patient.name,
                "type": appt.type,
                "therapist": "",
            })

    # Pending evolutions: past appointments without session on that date
    past_appts_result = await db.execute(
        select(Appointment)
        .where(
            Appointment.professional_id == professional_id,
            Appointment.status.in_(["pendente", "confirmado"]),
            Appointment.date <= today,
        )
    )
    past_appts = [
        appt
        for appt in past_appts_result.scalars().all()
        if appt.date < today or is_appointment_past(appt.date, appt.time, now)
    ]
    past_pairs = {(appt.patient_id, appt.date) for appt in past_appts}
    sessions_past = await _patients_with_session_on_dates(db, professional_id, past_pairs)
    evolutions_pending = sum(
        1 for appt in past_appts if (appt.patient_id, appt.date) not in sessions_past
    )

    awaiting_clause = Assessment.result.ilike("%aguardando%")
    assessment_counts = (
        await db.execute(
            select(
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
            )
            .select_from(Assessment)
            .join(Patient, Assessment.patient_id == Patient.id)
            .where(Patient.professional_id == professional_id)
        )
    ).one()
    assessment_drafts = int(assessment_counts[0] or 0)
    awaiting_informant = int(assessment_counts[1] or 0)

    pending = {
        "evolutions": evolutions_pending,
        "reports": reports_draft or 0,
        "sessions": sessions_pending or 0,
        "assessmentDrafts": assessment_drafts,
        "awaitingInformant": awaiting_informant,
    }
    suggestions = build_suggestions(pending)

    # Patient evolution placeholder from domain snapshots avg
    patient_evolution = [
        {"month": "Jan", "vocabulario": 42, "pragmatica": 30},
        {"month": "Fev", "vocabulario": 48, "pragmatica": 35},
        {"month": "Mar", "vocabulario": 55, "pragmatica": 41},
        {"month": "Abr", "vocabulario": 61, "pragmatica": 50},
        {"month": "Mai", "vocabulario": 68, "pragmatica": 58},
        {"month": "Jun", "vocabulario": 74, "pragmatica": 65},
    ]

    return {
        "kpis": {
            "activePatients": active_patients or 0,
            "newThisMonth": new_this_month or 0,
            "sessionsDone": sessions_done or 0,
            "sessionsPending": sessions_pending or 0,
            "aiReports": ai_reports or 0,
        },
        "patientEvolution": patient_evolution,
        "monthlyGrowth": monthly_growth,
        "upcomingAppointments": upcoming,
        "protocolsApplied": protocols_applied,
        "todayAgenda": today_agenda,
        "pending": pending,
        "suggestions": suggestions,
    }
