"""Public, signed attendance response flow for 24h WhatsApp reminders."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.notification_message_log import NotificationMessageLog
from app.models.notification_settings import NotificationSettings
from app.services.appointment_response_service import (
    APPOINTMENT_RESPONSE_TOKEN_TYPE,
    build_appointment_response_url,
    create_appointment_response_token,
)


@pytest.fixture(autouse=True)
def stable_clinic_timezone(monkeypatch):
    """Windows test env has no IANA tzdata; production containers do."""
    clinic_timezone = timezone(timedelta(hours=-3))
    monkeypatch.setattr(
        "app.services.appointment_response_service.ZoneInfo",
        lambda _key: clinic_timezone,
    )


def _appointment(professional_id, patient_id, *, days_from_today: int = 2) -> Appointment:
    return Appointment(
        id=uuid4(),
        professional_id=professional_id,
        patient_id=patient_id,
        date=date.today() + timedelta(days=days_from_today),
        time=time(10, 30),
        type="Terapia individual",
        duration=50,
        status="pendente",
    )


@pytest.mark.parametrize("offset,expected", [(-1, 200), (0, 410), (1, 410)])
@pytest.mark.parametrize("action", ["confirm", "cancel"])
async def test_previous_day_deadline_enforced_on_preview_and_submit(
    api_client, db_session, professional, patient, monkeypatch, offset, expected, action
):
    appointment = _appointment(professional.id, patient.id)
    appointment.date = date(2027, 1, 1)
    db_session.add_all([
        appointment,
        NotificationSettings(
            professional_id=professional.id,
            appointment_confirmation_deadline_time="20:00",
        ),
    ])
    await db_session.commit()
    # 20:00 in the clinic is 23:00 UTC, across the year boundary.
    now = datetime(2026, 12, 31, 23, tzinfo=UTC) + timedelta(seconds=offset)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr("app.services.appointment_response_service.datetime", FrozenDateTime)
    token = create_appointment_response_token(appointment)
    preview = await api_client.post("/api/v1/appointment-responses/preview", json={"token": token})
    response = await api_client.post(
        "/api/v1/appointment-responses", json={"token": token, "action": action}
    )
    assert preview.status_code == expected
    assert response.status_code == expected
    await db_session.refresh(appointment)
    if expected == 410:
        assert "prazo" in response.json()["detail"]
        assert appointment.status == "pendente"
        assert (await db_session.execute(select(NotificationMessageLog))).scalars().all() == []
    else:
        assert appointment.status == ("confirmado" if action == "confirm" else "cancelado")


@pytest.mark.asyncio
async def test_public_preview_and_confirm_update_the_appointment_without_auth(
    api_client, db_session, professional, patient
):
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()

    token = create_appointment_response_token(appointment)
    preview = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": token},
    )

    assert preview.status_code == 200
    assert preview.json() == {
        "appointmentDate": appointment.date.isoformat(),
        "appointmentTime": "10:30",
        "professionalName": professional.name,
        "status": "pendente",
        "canRespond": True,
    }
    assert "patient" not in preview.text.lower()

    response = await api_client.post(
        "/api/v1/appointment-responses",
        json={"token": token, "action": "confirm"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "confirmado"
    await db_session.refresh(appointment)
    assert appointment.status == "confirmado"


async def test_current_deadline_applies_to_existing_link_and_can_be_disabled(
    api_client, db_session, professional, patient, monkeypatch
):
    appointment = _appointment(professional.id, patient.id)
    appointment.date = date(2027, 1, 1)
    db_session.add(appointment)
    await db_session.commit()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 31, 23, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("app.services.appointment_response_service.datetime", FrozenDateTime)
    token = create_appointment_response_token(appointment)
    preview = await api_client.post("/api/v1/appointment-responses/preview", json={"token": token})
    assert preview.status_code == 200
    settings = NotificationSettings(
        professional_id=professional.id, appointment_confirmation_deadline_time="20:00"
    )
    db_session.add(settings)
    await db_session.commit()
    denied = await api_client.post(
        "/api/v1/appointment-responses", json={"token": token, "action": "confirm"}
    )
    assert denied.status_code == 410
    await db_session.refresh(appointment)
    assert appointment.status == "pendente"
    settings.appointment_confirmation_deadline_time = None
    await db_session.commit()
    accepted = await api_client.post(
        "/api/v1/appointment-responses", json={"token": token, "action": "confirm"}
    )
    assert accepted.status_code == 200


@pytest.mark.asyncio
async def test_public_cancel_is_idempotent_and_cannot_be_reversed_by_the_link(
    api_client, db_session, professional, patient
):
    appointment = _appointment(professional.id, patient.id)
    appointment.status = "confirmado"
    db_session.add(appointment)
    await db_session.commit()
    token = create_appointment_response_token(appointment)

    first = await api_client.post(
        "/api/v1/appointment-responses",
        json={"token": token, "action": "cancel"},
    )
    repeated = await api_client.post(
        "/api/v1/appointment-responses",
        json={"token": token, "action": "cancel"},
    )
    reversal = await api_client.post(
        "/api/v1/appointment-responses",
        json={"token": token, "action": "confirm"},
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "cancelado"
    assert reversal.status_code == 409
    await db_session.refresh(appointment)
    assert appointment.status == "cancelado"
    notification_logs = (
        await db_session.execute(select(NotificationMessageLog))
    ).scalars().all()
    assert notification_logs == []


@pytest.mark.asyncio
async def test_rescheduled_or_expired_appointment_invalidates_the_link(
    api_client, db_session, professional, patient
):
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()
    stale_token = create_appointment_response_token(appointment)

    appointment.time = time(11, 0)
    await db_session.commit()
    stale = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": stale_token},
    )

    expired_appointment = _appointment(professional.id, patient.id, days_from_today=-1)
    db_session.add(expired_appointment)
    await db_session.commit()
    expired = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": create_appointment_response_token(expired_appointment)},
    )

    assert stale.status_code == 410
    assert expired.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["x" * 40, "!" * 44])
async def test_invalid_token_is_rejected_without_database_details(api_client, token):
    response = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": token},
    )

    assert response.status_code == 410
    assert response.json() == {"detail": "Link inválido ou expirado."}


@pytest.mark.asyncio
async def test_tampered_compact_token_is_rejected(
    api_client, db_session, professional, patient
):
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()
    token = create_appointment_response_token(appointment)
    replacement = "A" if token[-1] != "A" else "B"

    response = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": f"{token[:-1]}{replacement}"},
    )

    assert response.status_code == 410
    assert response.json() == {"detail": "Link inválido ou expirado."}


@pytest.mark.asyncio
async def test_previously_issued_jwt_link_remains_valid(
    api_client, db_session, professional, patient
):
    appointment = _appointment(professional.id, patient.id)
    db_session.add(appointment)
    await db_session.commit()
    settings = get_settings()
    starts_at = datetime.combine(
        appointment.date,
        appointment.time,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    legacy_token = jwt.encode(
        {
            "sub": str(appointment.id),
            "type": APPOINTMENT_RESPONSE_TOKEN_TYPE,
            "professional_id": str(appointment.professional_id),
            "scheduled_date": appointment.date.isoformat(),
            "scheduled_time": appointment.time.isoformat(),
            "iat": datetime.now(UTC),
            "exp": starts_at.astimezone(UTC),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await api_client.post(
        "/api/v1/appointment-responses/preview",
        json={"token": legacy_token},
    )

    assert response.status_code == 200
    assert response.json()["appointmentDate"] == appointment.date.isoformat()


@pytest.mark.asyncio
async def test_response_url_keeps_the_token_out_of_the_query_string(
    professional, patient, monkeypatch
):
    appointment = _appointment(professional.id, patient.id)
    monkeypatch.setattr(
        "app.services.appointment_response_service.get_settings",
        lambda: type("Settings", (), {
            "frontend_url": "https://app.korusfono.com.br/",
            "jwt_secret": "test-secret-for-pytest-only-not-for-prod",
            "jwt_algorithm": "HS256",
            "clinic_timezone": "America/Sao_Paulo",
        })(),
    )

    url = urlsplit(build_appointment_response_url(appointment))

    assert url.scheme == "https"
    assert url.netloc == "app.korusfono.com.br"
    assert url.path == "/confirmar-consulta"
    assert url.query == ""
    assert url.fragment.startswith("token=")
    token = url.fragment.removeprefix("token=")
    assert len(token) == 44
    assert "." not in token
    assert token.replace("-", "").replace("_", "").isalnum()
    assert len(url.geturl()) < 120
