from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.patient import Patient
from app.models.professional import Professional
from app.services.dashboard import build_suggestions, derive_agenda_status

NOW = datetime(2026, 7, 9, 14, 0)
TODAY = date(2026, 7, 9)


def test_status_confirmado_futuro_mantido():
    assert derive_agenda_status("confirmado", TODAY, time(15, 0), False, NOW) == "confirmado"


def test_status_pendente_futuro_mantido():
    assert derive_agenda_status("pendente", TODAY, time(15, 0), False, NOW) == "pendente"


def test_horario_passado_sem_sessao_vira_evolucao_pendente():
    assert derive_agenda_status("confirmado", TODAY, time(10, 0), False, NOW) == "evolucao_pendente"
    assert derive_agenda_status("pendente", TODAY, time(10, 0), False, NOW) == "evolucao_pendente"


def test_horario_passado_com_sessao_mantem_status():
    assert derive_agenda_status("confirmado", TODAY, time(10, 0), True, NOW) == "confirmado"


def test_status_cancelado_nao_derivado():
    assert derive_agenda_status("cancelado", TODAY, time(10, 0), False, NOW) == "cancelado"


def test_suggestions_vazia_sem_pendencias():
    assert (
        build_suggestions(
            {
                "evolutions": 0,
                "reports": 0,
                "sessions": 0,
                "assessmentDrafts": 0,
                "awaitingInformant": 0,
            }
        )
        == []
    )


def test_suggestions_completa_com_ctas():
    result = build_suggestions(
        {
            "evolutions": 2,
            "reports": 1,
            "sessions": 3,
            "assessmentDrafts": 4,
            "awaitingInformant": 1,
        }
    )
    assert [s["id"] for s in result] == [
        "pending-evolutions",
        "pending-assessment-drafts",
        "pending-awaiting-informant",
        "pending-reports",
        "pending-sessions",
    ]
    assert result[0]["ctaTo"] == "/agenda"
    assert result[1]["ctaTo"] == "/avaliacoes?status=draft"
    assert result[2]["ctaTo"] == "/avaliacoes?status=awaiting_informant"
    assert result[3]["ctaTo"] == "/relatorios"
    assert result[4]["ctaTo"] == "/agenda"
    assert "2 evoluções" in result[0]["text"]
    assert "4 avaliações em rascunho" in result[1]["text"]
    assert "1 relatório em rascunho" in result[3]["text"]


async def test_dashboard_retorna_apenas_aniversariantes_do_profissional(
    api_client,
    auth_headers,
    db_session: AsyncSession,
    professional: Professional,
):
    clinic_today = datetime.now(ZoneInfo(get_settings().clinic_timezone)).date()
    birthday_patient = Patient(
        professional_id=professional.id,
        name="Ana Aniversariante",
        birth_date=clinic_today.replace(year=clinic_today.year - 4),
        diagnosis_keys=["linguagem"],
        status="ativo",
        start_date=clinic_today,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(birthday_patient)
    other_professional = Professional(
        email="other-dashboard@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CRFa",
        phone="11999990001",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(other_professional)
    await db_session.flush()
    db_session.add(
        Patient(
            professional_id=other_professional.id,
            name="Paciente de outra profissional",
            birth_date=clinic_today.replace(year=clinic_today.year - 7),
            diagnosis_keys=["linguagem"],
            status="ativo",
            start_date=clinic_today,
            avatar_color="oklch(0.60 0.10 160)",
        )
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/dashboard", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["birthdaysToday"] == [
        {
            "patientId": str(birthday_patient.id),
            "patientName": birthday_patient.name,
            "age": 4,
            "avatarColor": birthday_patient.avatar_color,
        }
    ]
