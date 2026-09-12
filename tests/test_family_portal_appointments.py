"""F14 onda 2 — agenda projetada do portal (§3.4).

Cobre: permissão por destinatário (``appointmentsEnabled`` false devolve o
mesmo envelope vazio sem consulta), corte [agora, agora + 60 dias] com fuso
da clínica, status reais (só ``pendente``/``confirmado``), escopo dono/paciente
(consulta de outro dono ou de outro paciente nunca entra), rótulo fixo,
conversão UTC de início/fim, ausência de serviço/preço/tipo/série/notas e
paginação. SQLite em memória; as corridas reais ficam no gate PostgreSQL.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.security import create_access_token, hash_password
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional

TODAY = date.today()
TOKEN_HEADER = "X-Family-Portal-Token"
PUBLIC = "/api/v1/family-portal"
CLINIC_TZ = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(autouse=True)
def allow_public_rate_limit(monkeypatch):
    """Domínio não depende de Redis: contador público sempre 'permite'."""
    monkeypatch.setattr(
        "app.services.clinical_public_rate_limit._redis_allow", lambda **_: True
    )


def _headers(professional: Professional) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(professional.id)}"}


async def _professional(db_session, *, email: str, name: str = "Dra. Apoio") -> Professional:
    professional = Professional(
        email=email,
        password_hash=hash_password("testpass123"),
        name=name,
        specialty_key="fono",
        specialty="Fonoaudiologia",
        phone="11999990000",
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(professional)
    await db_session.commit()
    await db_session.refresh(professional)
    return professional


async def _caregiver(
    db_session, patient: Patient, *, name: str = "Responsável Teste"
) -> Caregiver:
    caregiver = Caregiver(patient_id=patient.id, name=name, relation="Mãe")
    db_session.add(caregiver)
    await db_session.commit()
    await db_session.refresh(caregiver)
    return caregiver


async def _appointment(
    db_session,
    patient: Patient,
    professional: Professional,
    *,
    days_ahead: int,
    hour: int = 14,
    minute: int = 0,
    status_value: str = "pendente",
    duration: int = 50,
    appointment_type: str = "avulso",
) -> Appointment:
    appointment = Appointment(
        professional_id=professional.id,
        patient_id=patient.id,
        date=(datetime.now(UTC) + timedelta(days=days_ahead)).date(),
        time=time(hour, minute),
        type="Terapia de linguagem",
        duration=duration,
        status=status_value,
        appointment_type=appointment_type,
        service_name_snapshot="Sessão premium",
        service_price_cents=12345,
    )
    db_session.add(appointment)
    await db_session.commit()
    await db_session.refresh(appointment)
    return appointment


def _base(patient: Patient) -> str:
    return f"/api/v1/patients/{patient.id}/family-portal"


async def _enable(api_client, headers, patient: Patient) -> dict:
    response = await api_client.put(
        _base(patient),
        headers=headers,
        json={"enabled": True, "expectedVersion": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _authorize(
    api_client, headers, patient: Patient, caregiver_id, *, appointments: bool
) -> dict:
    response = await api_client.put(
        f"{_base(patient)}/recipients/{caregiver_id}",
        headers=headers,
        json={
            "appointmentsEnabled": appointments,
            "familyAuthorization": {
                "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "reference": "Termo",
                "reviewed": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _issue(api_client, headers, patient: Patient, recipient_id) -> str:
    response = await api_client.post(
        f"{_base(patient)}/recipients/{recipient_id}/grants",
        headers=headers,
        json={
            "expiresInDays": 30,
            "expectedRecipientVersion": 1,
            "rotateFromGrantId": None,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["url"].split("#token=", 1)[1]


async def _appointments(api_client, token: str, query: str = ""):
    return await api_client.get(
        f"{PUBLIC}/appointments{query}", headers={TOKEN_HEADER: token}
    )


def _expected_start(appointment_date: date, appointment_time: time) -> datetime:
    return datetime.combine(appointment_date, appointment_time, tzinfo=CLINIC_TZ)


async def test_appointments_projection_filters_and_allowlist(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(
        api_client, headers, patient, caregiver.id, appointments=True
    )
    token = await _issue(api_client, headers, patient, recipient["id"])

    pending = await _appointment(
        db_session, patient, professional, days_ahead=3, hour=14, status_value="pendente"
    )
    confirmed = await _appointment(
        db_session,
        patient,
        professional,
        days_ahead=10,
        hour=9,
        status_value="confirmado",
    )
    await _appointment(
        db_session, patient, professional, days_ahead=-2, status_value="confirmado"
    )
    await _appointment(
        db_session, patient, professional, days_ahead=5, status_value="cancelado"
    )
    await _appointment(
        db_session, patient, professional, days_ahead=5, status_value="concluido"
    )
    await _appointment(
        db_session, patient, professional, days_ahead=5, status_value="falta"
    )
    await _appointment(db_session, patient, professional, days_ahead=62)

    # Consulta de OUTRO dono para o mesmo paciente e do MESMO dono para outro
    # paciente nunca entram na projeção.
    foreign_professional = await _professional(
        db_session, email="outro@example.com", name="Dr. Fora"
    )
    await _appointment(db_session, patient, foreign_professional, days_ahead=4)
    other_patient = Patient(
        professional_id=professional.id,
        name="Outra criança",
        birth_date=date(2020, 1, 1),
        diagnosis_keys=[],
        status="ativo",
        start_date=TODAY,
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(other_patient)
    await db_session.commit()
    await db_session.refresh(other_patient)
    await _appointment(db_session, other_patient, professional, days_ahead=4)

    response = await _appointments(api_client, token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2 and body["page"] == 1 and body["limit"] == 20
    by_id = {item["id"]: item for item in body["items"]}
    assert set(by_id.keys()) == {str(pending.id), str(confirmed.id)}
    # Ordem por início crescente.
    assert [item["id"] for item in body["items"]] == [
        str(pending.id),
        str(confirmed.id),
    ]

    item = by_id[str(pending.id)]
    assert set(item.keys()) == {
        "id",
        "label",
        "startsAt",
        "endsAt",
        "timezone",
        "status",
    }
    assert item["label"] == "Sessão de fonoaudiologia"
    assert item["timezone"] == "America/Sao_Paulo"
    assert item["status"] == "pendente"
    expected_start = _expected_start(pending.date, pending.time)
    assert datetime.fromisoformat(item["startsAt"].replace("Z", "+00:00")) == (
        expected_start.astimezone(UTC)
    )
    assert datetime.fromisoformat(item["endsAt"].replace("Z", "+00:00")) == (
        expected_start.astimezone(UTC) + timedelta(minutes=50)
    )

    # Nenhum dado clínico/financeiro/operacional vaza para a família.
    for forbidden in (
        "serviceNameSnapshot",
        "Sessão premium",
        "servicePriceCents",
        "12345",
        "Terapia de linguagem",
        "appointmentType",
        "seriesId",
        "weekdays",
        "notes",
        "patientId",
        "professionalId",
        "outra criança",
        str(other_patient.id),
    ):
        assert forbidden not in response.text

    # Paginação: total conta só os autorizados.
    page = await _appointments(api_client, token, "?page=1&limit=1")
    assert page.json()["total"] == 2 and len(page.json()["items"]) == 1
    page2 = await _appointments(api_client, token, "?page=2&limit=1")
    assert page2.json()["items"][0]["id"] == str(confirmed.id)
    invalid = await _appointments(api_client, token, "?limit=51")
    assert invalid.status_code == 422

    # Reagendar/cancelar na agenda refletem o estado atual na releitura.
    pending.date = (datetime.now(UTC) + timedelta(days=20)).date()
    pending.time = time(8, 30)
    await db_session.commit()
    confirmed.status = "cancelado"
    await db_session.commit()
    refreshed = (await _appointments(api_client, token)).json()
    assert refreshed["total"] == 1
    moved = refreshed["items"][0]
    assert moved["id"] == str(pending.id)
    assert datetime.fromisoformat(moved["startsAt"].replace("Z", "+00:00")) == (
        _expected_start(pending.date, pending.time).astimezone(UTC)
    )


async def test_appointments_permission_toggles_per_recipient(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver_a = await _caregiver(db_session, patient, name="Ana")
    caregiver_b = await _caregiver(db_session, patient, name="Bia")
    recipient_a = await _authorize(
        api_client, headers, patient, caregiver_a.id, appointments=True
    )
    recipient_b = await _authorize(
        api_client, headers, patient, caregiver_b.id, appointments=False
    )
    token_a = await _issue(api_client, headers, patient, recipient_a["id"])
    token_b = await _issue(api_client, headers, patient, recipient_b["id"])
    await _appointment(db_session, patient, professional, days_ahead=2)

    body_a = (await _appointments(api_client, token_a)).json()
    body_b = (await _appointments(api_client, token_b)).json()
    assert body_a["total"] == 1
    assert body_b == {"items": [], "total": 0, "page": 1, "limit": 20}

    # Raiz pública reflete a permissão por destinatário.
    root_b = await api_client.get(
        PUBLIC, headers={TOKEN_HEADER: token_b}
    )
    assert root_b.json()["appointmentsEnabled"] is False
    root_a = await api_client.get(PUBLIC, headers={TOKEN_HEADER: token_a})
    assert root_a.json()["appointmentsEnabled"] is True

    # Liga a agenda da B e desliga a da A (ações versionadas e protetivas).
    enabled = await api_client.patch(
        f"{_base(patient)}/recipients/{recipient_b['id']}",
        headers=headers,
        json={"expectedVersion": 1, "appointmentsEnabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    off = await api_client.delete(
        f"{_base(patient)}/recipients/{recipient_a['id']}/appointments",
        headers=headers,
    )
    assert off.status_code == 204
    assert (await _appointments(api_client, token_b)).json()["total"] == 1
    assert (await _appointments(api_client, token_a)).json()["total"] == 0


async def test_appointments_endpoint_uses_generic_410_for_bad_links(
    api_client, auth_headers, db_session, patient, professional
):
    headers = auth_headers
    await _enable(api_client, headers, patient)
    caregiver = await _caregiver(db_session, patient)
    recipient = await _authorize(
        api_client, headers, patient, caregiver.id, appointments=True
    )
    token = await _issue(api_client, headers, patient, recipient["id"])
    await _appointment(db_session, patient, professional, days_ahead=1)

    missing = await api_client.get(f"{PUBLIC}/appointments")
    unknown = await api_client.get(
        f"{PUBLIC}/appointments", headers={TOKEN_HEADER: "desconhecido"}
    )
    revoked = await api_client.get(
        f"{PUBLIC}/appointments", headers={TOKEN_HEADER: token + "x"}
    )
    assert missing.status_code == 410
    assert unknown.status_code == 410
    assert revoked.status_code == 410
    expected = (
        "Este link está inválido ou indisponível. Peça um novo link à "
        "profissional."
    )
    assert missing.json()["detail"] == expected
    assert unknown.json()["detail"] == expected
