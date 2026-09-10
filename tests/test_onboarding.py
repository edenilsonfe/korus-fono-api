from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.models.ai import AIReport
from app.models.patient import Patient
from app.models.professional import Professional


async def _demo_patient(db_session, professional):
    from app.services.demo_patient_service import ensure_demo_patient
    demo = await ensure_demo_patient(db_session, professional)
    await db_session.commit()
    return demo


async def _action(client, headers, action, **fields):
    return await client.patch('/api/v1/me/activation', headers=headers, json={'action': action, **fields})


async def test_activation_starts_with_three_demo_moments(api_client, db_session, professional, auth_headers):
    demo = await _demo_patient(db_session, professional)
    response = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    assert response.status_code == 200
    state = response.json()
    assert state['version'] == 3
    assert state['demoPatientId'] == str(demo.id)
    assert state['demoStartedAt'] is None
    assert state['demoCompletedAt'] is None
    assert state['demoReportId'] is None
    assert state['isDemoComplete'] is False
    assert state['isComplete'] is False
    assert state['nextStep'] == 'view_demo_patient'
    assert not any(state['steps'].values())  # Seeded assessments do not count as M-CHAT practice.


async def test_activation_recreates_demo_idempotently(api_client, db_session, professional, auth_headers):
    first = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    second = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    assert first.status_code == second.status_code == 200
    assert first.json()['demoPatientId'] == second.json()['demoPatientId']
    demos = (await db_session.scalars(select(Patient).where(Patient.professional_id == professional.id, Patient.is_demo.is_(True)))).all()
    assert len(demos) == 1


async def test_demo_cannot_be_deleted_during_onboarding(api_client, db_session, professional, auth_headers):
    demo = await _demo_patient(db_session, professional)
    response = await api_client.delete(f'/api/v1/patients/{demo.id}', headers=auth_headers)
    assert response.status_code == 409


async def test_demo_views_skip_and_resume_preserve_first_timestamps(api_client, db_session, professional, auth_headers):
    await _demo_patient(db_session, professional)
    first = await _action(api_client, auth_headers, 'viewed_demo_patient')
    assert first.status_code == 200
    assert first.json()['nextStep'] == 'view_demo_result'
    assert first.json()['demoStartedAt'] is not None
    again = await _action(api_client, auth_headers, 'viewed_demo_patient')
    assert again.json()['demoStartedAt'] == first.json()['demoStartedAt']
    viewed = await _action(api_client, auth_headers, 'viewed_demo_result')
    assert viewed.status_code == 200
    assert viewed.json()['nextStep'] == 'create_demo_report'
    assert viewed.json()['steps']['completedDemoAssessment'] is False
    postponed = await _action(api_client, auth_headers, 'postpone')
    assert postponed.json()['dismissedUntil'] is not None
    skipped = await _action(api_client, auth_headers, 'skip')
    assert skipped.json()['skippedAt'] is not None
    resumed = await _action(api_client, auth_headers, 'resume')
    assert resumed.json()['dismissedUntil'] is None
    assert resumed.json()['skippedAt'] is None
    assert resumed.json()['demoStartedAt'] == first.json()['demoStartedAt']
    assert resumed.json()['nextStep'] == 'create_demo_report'


async def test_evolution_requires_viewing_the_case_not_mchat(api_client, db_session, professional, auth_headers):
    await _demo_patient(db_session, professional)
    response = await _action(api_client, auth_headers, 'viewed_demo_result')
    assert response.status_code == 409
    await _action(api_client, auth_headers, 'viewed_demo_patient')
    response = await _action(api_client, auth_headers, 'viewed_demo_result')
    assert response.status_code == 200
    assert response.json()['steps']['completedDemoAssessment'] is False


async def test_real_patient_does_not_complete_demo(api_client, db_session, professional, auth_headers):
    db_session.add(Patient(professional_id=professional.id, name='Paciente real', birth_date=date(2021, 5, 20), diagnosis_keys=[], status='avaliacao', start_date=date.today(), avatar_color='teal', is_demo=False))
    await db_session.commit()
    response = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    assert response.status_code == 200
    state = response.json()
    assert state['demoPatientId'] is None
    assert state['isDemoComplete'] is False
    assert state['demoCompletedAt'] is None
    assert state['steps']['createdRealPatient'] is True
    assert not any(value for key, value in state['steps'].items() if key != 'createdRealPatient')
    assert state['nextStep'] == 'configure_service'
    # Explicit resume is available even to professionals already using real patients.
    resumed = await _action(api_client, auth_headers, 'resume')
    assert resumed.status_code == 200
    assert resumed.json()['demoPatientId'] is not None
    assert resumed.json()['steps']['viewedDemoPatient'] is False


async def test_demo_requires_explicit_report_review_and_real_use_is_separate(api_client, db_session, professional, auth_headers):
    demo = await _demo_patient(db_session, professional)
    report = AIReport(professional_id=professional.id, patient_id=demo.id, type='clinical', date=date.today(), preview='Rascunho', content='Conteúdo demonstrativo', status='draft')
    db_session.add(report)
    await db_session.commit()
    await _action(api_client, auth_headers, 'viewed_demo_patient')
    viewed = await _action(api_client, auth_headers, 'viewed_demo_result')
    assert viewed.json()['nextStep'] == 'review_demo_report'
    assert viewed.json()['demoReportId'] == str(report.id)
    assert viewed.json()['isDemoComplete'] is False
    revised = await api_client.patch(
        f'/api/v1/ai/reports/{report.id}', headers=auth_headers,
        json={'content': 'Rascunho revisado durante a demonstração'},
    )
    assert revised.status_code == 200
    assert revised.json()['status'] == 'draft'
    reviewed = await _action(api_client, auth_headers, 'reviewed_demo_report', reportId=str(report.id))
    assert reviewed.status_code == 200
    state = reviewed.json()
    assert state['isDemoComplete'] is True
    assert state['isComplete'] is False
    assert state['nextStep'] == 'create_real_patient'
    assert state['demoCompletedAt'] is not None
    assert state['steps']['completedDemoAssessment'] is False
    repeated = await _action(api_client, auth_headers, 'reviewed_demo_report', reportId=str(report.id))
    assert repeated.json()['demoCompletedAt'] == state['demoCompletedAt']
    await db_session.refresh(report)
    assert report.status == 'draft'
    assert report.content == 'Rascunho revisado durante a demonstração'

    patient = await api_client.post('/api/v1/patients', headers=auth_headers, json={'name': 'Primeiro paciente', 'birthDate': '2021-05-20', 'diagnosisKeys': ['tea'], 'status': 'avaliacao', 'guardians': []})
    assert patient.status_code == 201
    after_patient = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    assert after_patient.json()['nextStep'] == 'configure_service'
    assert after_patient.json()['isComplete'] is False
    service = await api_client.post('/api/v1/finance/services', headers=auth_headers, json={'name': 'Terapia', 'duration': 50, 'priceCents': 18000})
    assert service.status_code == 201
    completed = await api_client.get('/api/v1/me/activation', headers=auth_headers)
    assert completed.json()['isComplete'] is True
    assert completed.json()['nextStep'] == 'completed'
    assert completed.json()['demoCompletedAt'] == state['demoCompletedAt']


async def test_review_rejects_missing_invalid_and_real_patient_reports(api_client, db_session, professional, auth_headers, patient):
    demo = await _demo_patient(db_session, professional)
    real_report = AIReport(professional_id=professional.id, patient_id=patient.id, type='clinical', date=date.today(), preview='Real', content='Texto', status='draft')
    demo_report = AIReport(professional_id=professional.id, patient_id=demo.id, type='clinical', date=date.today(), preview='Demo', content='Texto', status='draft')
    db_session.add_all([real_report, demo_report])
    await db_session.commit()
    other = Professional(email='other-onboarding@example.test', name='Outro profissional', password_hash='unused')
    db_session.add(other)
    await db_session.commit()
    other_demo = await _demo_patient(db_session, other)
    foreign_report = AIReport(professional_id=other.id, patient_id=other_demo.id, type='clinical', date=date.today(), preview='Outro caso', content='Texto de outra conta', status='draft')
    db_session.add(foreign_report)
    await db_session.commit()
    assert (await _action(api_client, auth_headers, 'reviewed_demo_report')).status_code == 422
    assert (await _action(api_client, auth_headers, 'reviewed_demo_report', reportId='invalid')).status_code == 422
    for report_id in (str(uuid4()), str(real_report.id), str(foreign_report.id)):
        assert (await _action(api_client, auth_headers, 'reviewed_demo_report', reportId=report_id)).status_code == 404
    assert (await _action(api_client, auth_headers, 'reviewed_demo_report', reportId=str(demo_report.id))).status_code == 409
    assert professional.onboarding_reviewed_demo_report_at is None
