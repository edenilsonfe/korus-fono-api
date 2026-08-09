from datetime import UTC, date, datetime, time
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.ai import AIReport
from app.models.assessment import Assessment
from app.models.goal import ClinicalDomainSnapshot
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
    monkeypatch,
):
    monkeypatch.setattr("app.services.dashboard.ZoneInfo", lambda _key: UTC)
    clinic_today = datetime.now(UTC).date()
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


async def test_dashboard_exclui_dados_demonstrativos_e_usa_evolucao_real(
    api_client,
    auth_headers,
    db_session: AsyncSession,
    professional: Professional,
    monkeypatch,
):
    monkeypatch.setattr("app.services.dashboard.ZoneInfo", lambda _key: UTC)
    clinic_today = datetime.now(UTC).date()
    real_patient = Patient(
        professional_id=professional.id,
        name="Paciente real do dashboard",
        birth_date=clinic_today.replace(year=clinic_today.year - 4),
        diagnosis_keys=["linguagem"],
        status="ativo",
        start_date=clinic_today,
        avatar_color="oklch(0.58 0.12 205)",
        is_demo=False,
    )
    demo_patient = Patient(
        professional_id=professional.id,
        name="Paciente demonstração",
        birth_date=clinic_today.replace(year=clinic_today.year - 2),
        diagnosis_keys=[],
        status="ativo",
        start_date=clinic_today,
        avatar_color="oklch(0.60 0.10 160)",
        is_demo=True,
    )
    db_session.add_all([real_patient, demo_patient])
    await db_session.flush()
    db_session.add_all(
        [
            Assessment(
                patient_id=real_patient.id,
                professional_id=professional.id,
                protocol_id="mchat",
                date=clinic_today,
                result="Concluído",
                percentage=50,
                interpretation="",
                fields=[],
                answers={},
                status="completed",
            ),
            Assessment(
                patient_id=demo_patient.id,
                professional_id=professional.id,
                protocol_id="mchat",
                date=clinic_today,
                result="Rascunho",
                percentage=0,
                interpretation="",
                fields=[],
                answers={"1": "sim"},
                status="draft",
            ),
            AIReport(
                professional_id=real_patient.professional_id,
                patient_id=real_patient.id,
                type="clinical",
                date=clinic_today,
                preview="Real",
                content="Real",
                status="final",
            ),
            AIReport(
                professional_id=demo_patient.professional_id,
                patient_id=demo_patient.id,
                type="clinical",
                date=clinic_today,
                preview="Demo",
                content="Demo",
                status="draft",
            ),
            ClinicalDomainSnapshot(
                patient_id=real_patient.id,
                key="vocabulario",
                label="Vocabulário",
                score=42,
                recorded_at=clinic_today,
            ),
            ClinicalDomainSnapshot(
                patient_id=real_patient.id,
                key="pragmatica",
                label="Pragmática",
                score=57,
                recorded_at=clinic_today,
            ),
            ClinicalDomainSnapshot(
                patient_id=demo_patient.id,
                key="vocabulario",
                label="Vocabulário",
                score=99,
                recorded_at=clinic_today,
            ),
        ]
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/dashboard", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["kpis"]["activePatients"] == 1
    assert payload["kpis"]["aiReports"] == 1
    assert payload["pending"]["assessmentDrafts"] == 0
    assert payload["pending"]["reports"] == 0
    assert payload["protocolsApplied"] == [{"name": "MCHAT", "value": 1}]
    assert [birthday["patientName"] for birthday in payload["birthdaysToday"]] == [
        real_patient.name
    ]
    month_label = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"][
        clinic_today.month - 1
    ]
    assert payload["patientEvolution"] == [
        {"month": month_label, "vocabulario": 42, "pragmatica": 57}
    ]


async def test_dashboard_sem_snapshots_reais_nao_exibe_evolucao_ficticia(
    api_client,
    auth_headers,
    monkeypatch,
):
    monkeypatch.setattr("app.services.dashboard.ZoneInfo", lambda _key: UTC)
    response = await api_client.get("/api/v1/dashboard", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["patientEvolution"] == []
