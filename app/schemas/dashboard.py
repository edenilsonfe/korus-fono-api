from app.schemas.common import CamelModel


class DashboardKpis(CamelModel):
    active_patients: int
    new_this_month: int
    sessions_done: int
    sessions_pending: int
    ai_reports: int


class DashboardPatientEvolution(CamelModel):
    month: str
    vocabulario: int
    pragmatica: int


class DashboardMonthlyGrowth(CamelModel):
    month: str
    pacientes: int


class DashboardUpcomingAppointment(CamelModel):
    id: str
    time: str
    patient_name: str
    type: str
    therapist: str


class DashboardProtocolApplied(CamelModel):
    name: str
    value: int


class DashboardTodayAgendaItem(CamelModel):
    id: str
    time: str
    patient_id: str
    patient_name: str
    type: str
    status: str


class DashboardPending(CamelModel):
    evolutions: int
    reports: int
    sessions: int
    assessment_drafts: int
    awaiting_informant: int


class DashboardSuggestion(CamelModel):
    id: str
    title: str
    text: str
    cta_label: str
    cta_to: str


class DashboardBirthday(CamelModel):
    patient_id: str
    patient_name: str
    age: int
    avatar_color: str


class DashboardResponse(CamelModel):
    kpis: DashboardKpis
    patient_evolution: list[DashboardPatientEvolution]
    monthly_growth: list[DashboardMonthlyGrowth]
    upcoming_appointments: list[DashboardUpcomingAppointment]
    protocols_applied: list[DashboardProtocolApplied]
    today_agenda: list[DashboardTodayAgendaItem]
    birthdays_today: list[DashboardBirthday]
    pending: DashboardPending
    suggestions: list[DashboardSuggestion]
