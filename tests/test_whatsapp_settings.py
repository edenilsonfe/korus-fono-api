"""Regression tests for per-professional WhatsApp message templates."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def patch_entitlement_session(db_engine, monkeypatch):
    """Bind the write-entitlement middleware to the test database."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.entitlement.AsyncSessionLocal", factory)


@pytest.mark.asyncio
async def test_reminder_template_survives_update_and_reload(api_client, auth_headers):
    custom_template = (
        "Oi, {{patientName}}! Seu atendimento com {{clinicianName}} será "
        "em {{appointmentDate}} às {{appointmentTime}}."
    )

    updated = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": custom_template,
            }
        },
    )

    assert updated.status_code == 200
    assert (
        updated.json()["whatsappMessageTemplates"]["appointment_reminder_24h"]
        == custom_template
    )
    assert updated.json()["templateDefaults"]["appointment_reminder_24h"]

    reloaded = await api_client.get(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
    )

    assert reloaded.status_code == 200
    assert (
        reloaded.json()["whatsappMessageTemplates"]["appointment_reminder_24h"]
        == custom_template
    )


@pytest.mark.asyncio
async def test_reminder_template_can_be_restored_to_default(api_client, auth_headers):
    await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": "Lembrete personalizado",
            }
        },
    )

    restored = await api_client.put(
        "/api/v1/whatsapp/settings",
        headers=auth_headers,
        json={
            "whatsappMessageTemplates": {
                "appointmentReminder24h": None,
            }
        },
    )

    assert restored.status_code == 200
    assert restored.json()["whatsappMessageTemplates"]["appointment_reminder_24h"] is None
    assert restored.json()["templateDefaults"]["appointment_reminder_24h"]
