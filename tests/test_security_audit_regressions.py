from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.ai import AIReport, AIReportRevision
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.refresh_session import RefreshSession
from app.models.session import Session


async def test_refresh_replay_commits_revocation(api_client, professional, db_engine, db_session):
    login = await api_client.post("/api/v1/auth/login", json={"email": professional.email, "password": "testpass123"})
    old = login.cookies["korus_refresh"]
    rotated = await api_client.post("/api/v1/auth/refresh", json={"refreshToken": old})
    assert rotated.status_code == 200
    previous = (await db_session.scalars(select(RefreshSession).where(
        RefreshSession.professional_id == professional.id,
        RefreshSession.revoked_at.is_not(None),
    ))).one()
    previous.revoked_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    api_client.cookies.clear()
    reused = await api_client.post("/api/v1/auth/refresh", json={"refreshToken": old})
    assert reused.status_code == 401
    async with AsyncSession(db_engine) as reader:
        saved = await reader.get(Professional, professional.id)
        sessions = (await reader.scalars(select(RefreshSession).where(RefreshSession.professional_id == saved.id))).all()
        assert saved.token_version == 1
        assert sessions and all(session.revoked_at for session in sessions)


async def test_evolution_requires_matching_session(api_client, patient, professional, db_session, auth_headers):
    other = Professional(email="other-audit@example.com", password_hash="unused", name="Other")
    db_session.add(other)
    await db_session.flush()
    other_patient = Patient(professional_id=other.id, name="Other patient", birth_date=date(2020, 1, 1), start_date=date.today(), diagnosis_keys=[], avatar_color="teal")
    db_session.add(other_patient)
    await db_session.flush()
    own = Session(professional_id=professional.id, patient_id=patient.id, date=datetime.now(UTC), duration=45, type="Terapia", objectives=[], notes="")
    foreign = Session(professional_id=other.id, patient_id=other_patient.id, date=datetime.now(UTC), duration=45, type="Terapia", objectives=[], notes="")
    db_session.add_all([own, foreign])
    await db_session.commit()
    path = f"/api/v1/patients/{patient.id}/evolutions"
    own_id, foreign_id = str(own.id), str(foreign.id)
    assert (await api_client.post(path, headers=auth_headers, json={"content": "Teste", "sessionId": foreign_id})).status_code == 404
    assert (await api_client.post(path, headers=auth_headers, json={"content": "Teste", "sessionId": "invalid"})).status_code == 422
    assert (await api_client.post(path, headers=auth_headers, json={"content": "Teste", "sessionId": own_id})).status_code == 201


async def test_finalized_report_preserves_history(api_client, patient, professional, db_session, auth_headers):
    report = AIReport(professional_id=professional.id, patient_id=patient.id, type="clinico", date=date.today(), preview="Original", content="Original", status="finalized")
    db_session.add(report)
    await db_session.commit()
    report_id = str(report.id)
    path = f"/api/v1/ai/reports/{report_id}"
    changed = await api_client.patch(path, headers=auth_headers, json={"content": "Revisado"})
    assert changed.status_code == 200
    history = await api_client.get(path + "/revisions", headers=auth_headers)
    assert history.status_code == 200
    assert history.json()[0]["content"] == "Original"
    assert history.json()[0]["status"] == "finalized"
    assert (await api_client.patch(path, headers=auth_headers, json={"content": "Revisado", "status": "draft"})).status_code == 409
    assert (await api_client.patch(path, headers=auth_headers, json={"content": "Revisado", "status": "inventado"})).status_code == 422
    assert (await api_client.get(f"/api/v1/ai/reports/{uuid4()}/revisions", headers=auth_headers)).status_code == 404


async def test_report_list_is_bounded_and_omits_content(api_client, patient, professional, db_session, auth_headers):
    db_session.add_all([AIReport(professional_id=professional.id, patient_id=patient.id, type="clinico", date=date.today(), preview="Resumo", content="Conteúdo clínico", status="draft") for _ in range(3)])
    await db_session.commit()
    rows = await api_client.get("/api/v1/ai/reports?limit=2", headers=auth_headers)
    assert len(rows.json()) == 2
    assert all(row["content"] == "" for row in rows.json())
    detail = await api_client.get(f"/api/v1/ai/reports/{rows.json()[0]['id']}", headers=auth_headers)
    assert detail.json()["content"] == "Conteúdo clínico"
    assert (await api_client.get("/api/v1/ai/reports?limit=201", headers=auth_headers)).status_code == 422


async def test_consent_revocation_without_verified_email(api_client, professional, db_session):
    professional.email_verified_at = None
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(professional.id)}"}
    for choice in [True, False]:
        response = await api_client.patch("/api/v1/me/analytics-consent", headers=headers, json={"analyticsConsent": choice})
        assert response.status_code == 200
        assert (await api_client.get("/api/v1/me", headers=headers)).json()["analyticsConsent"] is choice


async def test_sensitive_errors_and_reads_are_not_cacheable(api_client, auth_headers):
    for headers in [{}, auth_headers]:
        response = await api_client.get("/api/v1/patients", headers=headers)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
