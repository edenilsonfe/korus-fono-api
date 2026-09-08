"""Allowlisted query tools for the unified assistant (clínico + gestão).

Each tool is declared once via ``@_tool`` (schema + handler). ``TOOL_DEFINITIONS``
is derived from the registry, so adding a tool means writing a single handler.

Gestão tools are scoped to the logged-in professional. Clínico tools validate
that the patient belongs to the professional before returning any data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.assessment import Assessment
from app.models.evolution import Evolution
from app.models.goal import Goal
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.services.ai_service import build_patient_context

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 4

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler


_REGISTRY: Dict[str, ToolDef] = {}


def _tool(name: str, description: str, parameters: Dict[str, Any]) -> Callable[[ToolHandler], ToolHandler]:
    def decorator(fn: ToolHandler) -> ToolHandler:
        _REGISTRY[name] = ToolDef(name=name, description=description, parameters=parameters, handler=fn)
        return fn

    return decorator


TOOL_DEFINITIONS: List[Dict[str, Any]] = []  # populated in _build_definitions()


def _build_definitions() -> None:
    TOOL_DEFINITIONS.clear()
    for tool in _REGISTRY.values():
        TOOL_DEFINITIONS.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
        )


def parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    import json

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #


def _month_start(d: date = None) -> date:
    today = d or date.today()
    return today.replace(day=1)


def _coerce_date(value: Optional[str], default: Optional[date] = None) -> Optional[date]:
    if not value:
        return default
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# Gestão tools — scoped to professional.id
# --------------------------------------------------------------------------- #

_DATE_PROPS: Dict[str, Any] = {
    "date_from": {"type": "string", "description": "Data inicial YYYY-MM-DD (opcional)"},
    "date_to": {"type": "string", "description": "Data final YYYY-MM-DD (opcional)"},
}


@_tool(
    "get_dashboard_stats",
    "KPIs gerais da prática do profissional: pacientes ativos, novos no mês, sessões realizadas, "
    "atendimentos pendentes e relatórios IA gerados. Use para perguntas como 'como está minha clínica'.",
    {"type": "object", "properties": {}, "required": []},
)
async def _get_dashboard_stats(db: AsyncSession, professional: Professional, **_kw: Any) -> Dict[str, Any]:
    from app.services.dashboard import build_dashboard

    data = await build_dashboard(db, professional.id)
    return {"dashboard": data}


@_tool(
    "get_appointment_kpis",
    "KPIs de atendimentos do profissional por período: faltas (no_show), cancelamentos (cancellation), "
    "concluídos (completion) ou todos (all). Use date_from/date_to em YYYY-MM-DD; default = mês corrente.",
    {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["no_show", "cancellation", "completion", "all"],
                "description": "KPI: no_show (faltas), cancellation (cancelamentos), completion (concluídos), all (todos).",
            },
            **_DATE_PROPS,
        },
        "required": ["metric"],
    },
)
async def _get_appointment_kpis(
    db: AsyncSession,
    professional: Professional,
    *,
    metric: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    **_kw: Any,
) -> Dict[str, Any]:
    today = date.today()
    start = _coerce_date(date_from, _month_start(today))
    end = _coerce_date(date_to, today)

    # KPIs are derived from appointments joined with sessions (concluído = has a session).
    base = (
        select(Appointment)
        .where(
            Appointment.professional_id == professional.id,
            Appointment.date >= start,
            Appointment.date <= end,
        )
    )

    no_show = await db.scalar(
        select(func.count()).select_from(Appointment).where(
            Appointment.professional_id == professional.id,
            Appointment.status == "cancelado",
            Appointment.date >= start,
            Appointment.date <= end,
        )
    )
    # "concluído" = sessões realizadas no período (Session has the real attendance).
    completion = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(
            Session.professional_id == professional.id,
            func.date(Session.date) >= start,
            func.date(Session.date) <= end,
        )
    )
    pending = await db.scalar(
        select(func.count()).select_from(Appointment).where(
            Appointment.professional_id == professional.id,
            Appointment.status.in_(["pendente", "confirmado"]),
            Appointment.date >= today,
        )
    )

    result: Dict[str, Any] = {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "no_show": int(no_show or 0),
        "completion": int(completion or 0),
        "pending_upcoming": int(pending or 0),
    }
    if metric != "all":
        result = {k: v for k, v in result.items() if k in (metric, "date_from", "date_to")}
    return result


@_tool(
    "get_practice_trend",
    "Evolução temporal da prática: sessões por mês (sessions) ou novos pacientes por mês (new_patients). "
    "months = nº de meses a retroagir (padrão 6, máx 12).",
    {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["sessions", "new_patients"],
                "description": "Métrica: sessions (sessões realizadas por mês) ou new_patients (cadastros por mês).",
            },
            "months": {"type": "integer", "description": "Meses a retroagir (padrão 6, máx 12)"},
        },
        "required": ["metric"],
    },
)
async def _get_practice_trend(
    db: AsyncSession,
    professional: Professional,
    *,
    metric: str,
    months: int = 6,
    **_kw: Any,
) -> Dict[str, Any]:
    months = max(1, min(int(months or 6), 12))
    today = date.today()
    start = (today.replace(day=1) - timedelta(days=30 * (months - 1))).replace(day=1)

    series: List[Dict[str, Any]] = []
    if metric == "sessions":
        rows = await db.execute(
            select(func.date_trunc("month", Session.date).label("m"), func.count().label("c"))
            .where(
                Session.professional_id == professional.id,
                Session.date >= start,
            )
            .group_by("m")
            .order_by("m")
        )
        for m, c in rows.all():
            series.append({"month": str(m)[:7], "count": int(c)})
    elif metric == "new_patients":
        rows = await db.execute(
            select(func.date_trunc("month", Patient.start_date).label("m"), func.count().label("c"))
            .where(
                Patient.professional_id == professional.id,
                Patient.start_date >= start,
            )
            .group_by("m")
            .order_by("m")
        )
        for m, c in rows.all():
            series.append({"month": str(m)[:7], "count": int(c)})
    return {"metric": metric, "months": months, "series": series}


@_tool(
    "get_patient_ranking",
    "Ranking de pacientes do profissional por volume: appointments (mais atendimentos) ou no_shows (mais faltas). "
    "limit = nº no ranking (padrão 10, máx 20).",
    {
        "type": "object",
        "properties": {
            "sort_by": {
                "type": "string",
                "enum": ["appointments", "no_shows"],
                "description": "Critério: appointments (mais atendimentos) ou no_shows (mais faltas).",
            },
            "limit": {"type": "integer", "description": "Quantidade no ranking (padrão 10, máx 20)"},
        },
        "required": ["sort_by"],
    },
)
async def _get_patient_ranking(
    db: AsyncSession,
    professional: Professional,
    *,
    sort_by: str,
    limit: int = 10,
    **_kw: Any,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 10), 20))

    if sort_by == "appointments":
        rows = await db.execute(
            select(Patient.name, func.count(Session.id).label("c"))
            .join(Session, Session.patient_id == Patient.id)
            .where(Session.professional_id == professional.id)
            .group_by(Patient.id, Patient.name)
            .order_by(func.count(Session.id).desc())
            .limit(limit)
        )
        items = [{"patient": name, "count": int(c)} for name, c in rows.all()]
    else:  # no_shows — appointments cancelled
        rows = await db.execute(
            select(Patient.name, func.count(Appointment.id).label("c"))
            .join(Appointment, Appointment.patient_id == Patient.id)
            .where(
                Appointment.professional_id == professional.id,
                Appointment.status == "cancelado",
            )
            .group_by(Patient.id, Patient.name)
            .order_by(func.count(Appointment.id).desc())
            .limit(limit)
        )
        items = [{"patient": name, "count": int(c)} for name, c in rows.all()]
    return {"sort_by": sort_by, "ranking": items}


@_tool(
    "get_inactive_patients",
    "Pacientes do profissional sem sessão concluída há N dias (padrão 90, máx 365). "
    "Use para 'quais pacientes estão parados/inativos'.",
    {
        "type": "object",
        "properties": {
            "inactive_days": {
                "type": "integer",
                "description": "Dias sem sessão para considerar inativo (padrão 90, máx 365)",
            },
        },
        "required": [],
    },
)
async def _get_inactive_patients(
    db: AsyncSession,
    professional: Professional,
    *,
    inactive_days: int = 90,
    **_kw: Any,
) -> Dict[str, Any]:
    inactive_days = max(1, min(int(inactive_days or 90), 365))
    cutoff = date.today() - timedelta(days=inactive_days)

    # Patients of this professional whose latest session is before the cutoff
    # (or who never had a session). Uses a NOT EXISTS on recent sessions.
    rows = await db.execute(
        select(Patient.id, Patient.name, Patient.status)
        .where(
            Patient.professional_id == professional.id,
            Patient.status == "ativo",
            ~select(Session.id)
            .select_from(Session)
            .where(
                Session.patient_id == Patient.id,
                Session.professional_id == professional.id,
                func.date(Session.date) >= cutoff,
            )
            .exists(),
        )
        .order_by(Patient.name)
    )
    items = [{"id": str(pid), "name": name, "status": status} for pid, name, status in rows.all()]
    return {"inactive_days": inactive_days, "patients": items}


@_tool(
    "get_appointments_list",
    "Agenda do profissional: today (hoje), upcoming (próximos) ou recent (recentes). "
    "limit = nº de itens (padrão 10, máx 30).",
    {
        "type": "object",
        "properties": {
            "window": {
                "type": "string",
                "enum": ["today", "upcoming", "recent"],
                "description": "Janela: today (hoje), upcoming (próximos), recent (recentes).",
            },
            "limit": {"type": "integer", "description": "Quantidade (padrão 10, máx 30)"},
        },
        "required": ["window"],
    },
)
async def _get_appointments_list(
    db: AsyncSession,
    professional: Professional,
    *,
    window: str,
    limit: int = 10,
    **_kw: Any,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 10), 30))
    today = date.today()

    stmt = (
        select(Appointment, Patient.name)
        .join(Patient, Appointment.patient_id == Patient.id)
        .where(Appointment.professional_id == professional.id)
    )
    if window == "today":
        stmt = stmt.where(Appointment.date == today).order_by(Appointment.time)
    elif window == "upcoming":
        stmt = stmt.where(Appointment.date > today, Appointment.status.in_(["pendente", "confirmado"])).order_by(
            Appointment.date, Appointment.time
        )
    else:  # recent
        stmt = stmt.where(Appointment.date < today).order_by(Appointment.date.desc(), Appointment.time.desc())
    stmt = stmt.limit(limit)

    rows = await db.execute(stmt)
    items = [
        {
            "id": str(appt.id),
            "patient": name,
            "date": appt.date.isoformat(),
            "time": str(appt.time),
            "type": appt.type,
            "status": appt.status,
        }
        for appt, name in rows.all()
    ]
    return {"window": window, "appointments": items}


# --------------------------------------------------------------------------- #
# Clínico tools — validate patient ownership
# --------------------------------------------------------------------------- #


async def _get_owned_patient(db: AsyncSession, professional: Professional, patient_id: str) -> Optional[Patient]:
    try:
        pid = UUID(str(patient_id))
    except (ValueError, TypeError):
        return None
    patient = await db.get(Patient, pid)
    if patient is None or patient.professional_id != professional.id:
        return None
    return patient


@_tool(
    "search_patient_by_name",
    "Encontra pacientes do profissional pelo nome (busca parcial, case-insensitive). "
    "Use quando o usuário mencionar um paciente pelo nome e não houver patient_id vinculado à conversa. "
    "Retorna id + nome para usar em tools clínicas subsequentes.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nome (ou parte) do paciente"},
            "limit": {"type": "integer", "description": "Máximo de resultados (padrão 5, máx 10)"},
        },
        "required": ["name"],
    },
)
async def _search_patient_by_name(
    db: AsyncSession,
    professional: Professional,
    *,
    name: str,
    limit: int = 5,
    **_kw: Any,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 5), 10))
    pattern = f"%{name.strip()}%"
    rows = await db.execute(
        select(Patient.id, Patient.name, Patient.status)
        .where(
            Patient.professional_id == professional.id,
            Patient.name.ilike(pattern),
        )
        .order_by(Patient.name)
        .limit(limit)
    )
    items = [{"id": str(pid), "name": pname, "status": status} for pid, pname, status in rows.all()]
    return {"patients": items}


@_tool(
    "get_patient_context",
    "Snapshot completo de um paciente: dados, agregados (sessões, metas atingidas), metas recentes, "
    "evoluções recentes e avaliações recentes. Use para 'quadro geral do paciente' ou 'resumo do caso'.",
    {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string", "description": "UUID do paciente"},
        },
        "required": ["patient_id"],
    },
)
async def _get_patient_context_tool(
    db: AsyncSession,
    professional: Professional,
    *,
    patient_id: str,
    **_kw: Any,
) -> Dict[str, Any]:
    patient = await _get_owned_patient(db, professional, patient_id)
    if patient is None:
        return {"error": "Paciente não encontrado na sua carteira."}
    context = await build_patient_context(db, patient.id)
    return {"patient_id": str(patient.id), "patient_name": patient.name, "context": context}


@_tool(
    "get_patient_evolutions",
    "Últimas evoluções clínicas de um paciente. Use para 'como está a evolução do paciente'.",
    {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string", "description": "UUID do paciente"},
            "limit": {"type": "integer", "description": "Quantidade (padrão 5, máx 20)"},
        },
        "required": ["patient_id"],
    },
)
async def _get_patient_evolutions(
    db: AsyncSession,
    professional: Professional,
    *,
    patient_id: str,
    limit: int = 5,
    **_kw: Any,
) -> Dict[str, Any]:
    patient = await _get_owned_patient(db, professional, patient_id)
    if patient is None:
        return {"error": "Paciente não encontrado na sua carteira."}
    limit = max(1, min(int(limit or 5), 20))
    rows = await db.execute(
        select(Evolution)
        .where(Evolution.patient_id == patient.id)
        .order_by(Evolution.date.desc())
        .limit(limit)
    )
    items = [
        {"id": str(e.id), "date": e.date.date().isoformat(), "title": e.title, "content": e.content}
        for e in rows.scalars().all()
    ]
    return {"patient_id": str(patient.id), "patient_name": patient.name, "evolutions": items}


@_tool(
    "get_patient_goals",
    "Metas terapêuticas de um paciente e o progresso de cada uma. Use para 'metas do paciente' ou 'progresso das metas'.",
    {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string", "description": "UUID do paciente"},
        },
        "required": ["patient_id"],
    },
)
async def _get_patient_goals(
    db: AsyncSession,
    professional: Professional,
    *,
    patient_id: str,
    **_kw: Any,
) -> Dict[str, Any]:
    patient = await _get_owned_patient(db, professional, patient_id)
    if patient is None:
        return {"error": "Paciente não encontrado na sua carteira."}
    rows = await db.execute(
        select(Goal).where(Goal.patient_id == patient.id).order_by(Goal.start_date.desc())
    )
    items = [
        {
            "id": str(g.id),
            "title": g.title,
            "area": g.area,
            "progress": g.progress,
            "status": g.status,
            "start_date": g.start_date.isoformat(),
        }
        for g in rows.scalars().all()
    ]
    return {"patient_id": str(patient.id), "patient_name": patient.name, "goals": items}


@_tool(
    "get_patient_assessments",
    "Avaliações/instrumentos aplicados a um paciente e os resultados. Use para 'avaliações aplicadas' ou 'resultados dos instrumentos'.",
    {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string", "description": "UUID do paciente"},
            "limit": {"type": "integer", "description": "Quantidade (padrão 5, máx 20)"},
        },
        "required": ["patient_id"],
    },
)
async def _get_patient_assessments(
    db: AsyncSession,
    professional: Professional,
    *,
    patient_id: str,
    limit: int = 5,
    **_kw: Any,
) -> Dict[str, Any]:
    patient = await _get_owned_patient(db, professional, patient_id)
    if patient is None:
        return {"error": "Paciente não encontrado na sua carteira."}
    limit = max(1, min(int(limit or 5), 20))
    rows = await db.execute(
        select(Assessment)
        .where(Assessment.patient_id == patient.id)
        .order_by(Assessment.date.desc())
        .limit(limit)
    )
    items = [
        {
            "id": str(a.id),
            "protocol_id": a.protocol_id,
            "date": a.date.isoformat(),
            "result": a.result,
            "percentage": a.percentage,
            "interpretation": a.interpretation,
            "status": a.status,
        }
        for a in rows.scalars().all()
    ]
    return {"patient_id": str(patient.id), "patient_name": patient.name, "assessments": items}


# Build the OpenAI tool definitions after all handlers are registered.
_build_definitions()


# --------------------------------------------------------------------------- #
# ToolExecutor
# --------------------------------------------------------------------------- #


@dataclass
class ToolExecutor:
    db: AsyncSession
    professional: Professional
    tools_used: List[str] = field(default_factory=list)
    date_from: Optional[date] = None
    date_to: Optional[date] = None

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        tool = _REGISTRY.get(name)
        if tool is None:
            return {"error": f"Ferramenta desconhecida: {name}"}
        try:
            result = await tool.handler(self.db, self.professional, **args)
            self.tools_used.append(name)
            # Track date window from KPI/context tools when present.
            if isinstance(result, dict):
                df = result.get("date_from")
                dt = result.get("date_to")
                if df:
                    self.date_from = _coerce_date(df, self.date_from)
                if dt:
                    self.date_to = _coerce_date(dt, self.date_to)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("Assistant tool execution failed: %s", name)
            return {"error": "Não foi possível obter esses dados no momento."}
