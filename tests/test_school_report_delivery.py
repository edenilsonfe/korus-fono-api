"""F20 — school deliveries: authorization, minimized e-mail and derived revocation.

Behaviour under test (plan contract §3.2, task 2.1):

- Only the patient/report owner can deliver, only ``type=escolar`` +
  ``status=finalized`` reports, and only via ``email``/``link``
  (school + WhatsApp is rejected).
- A school delivery carries the minimal school recipient and a restricted
  authorization record (authorizing caregiver, at least one evidence and
  ``reviewed=true`` data do servidor); the F1 ``caregiverId`` never becomes the
  school contact and no caregiver is created/reused to bypass the API.
- The delivery + authorization is persisted before the e-mail I/O; the send
  result is a second transaction, so a crash after the provider accepted the
  message leaves ``created`` — never a false ``sent``.
- Evidence is exposed only in the owner's authenticated response.
"""

import copy
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.ai import AIReport
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.report_delivery import ReportDelivery
from app.schemas.report_delivery import ReportDeliveryCreate
from app.services import report_delivery_service

SCHOOL_AUTHORIZATION_KEYS = {
    "caregiverId",
    "authorizedAt",
    "evidenceReference",
    "evidenceAttachmentId",
    "reviewed",
    "purpose",
}
EVIDENCE_TEXT = "Termo de autorização assinado arquivado no prontuário"
SCHOOL_NAME = "Escola Municipal Vila Nova"
SCHOOL_RECIPIENT = "Coordenação Ana"


@pytest.fixture
async def escolar_report(db_session, professional, patient):
    report = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="escolar",
        date=date(2026, 9, 1),
        preview="Relatório escolar revisado",
        content="## Síntese\nTexto escolar revisado pela profissional.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


@pytest.fixture
async def caregiver(db_session, patient):
    return await db_session.scalar(select(Caregiver).where(Caregiver.patient_id == patient.id))


@pytest.fixture
def send_mock(monkeypatch):
    mock = MagicMock(return_value="email-msg-1")
    monkeypatch.setattr("app.services.school_report_delivery_service.send_email", mock)
    return mock


async def _add_second_patient(db_session, professional, name="Outro Paciente"):
    patient = Patient(
        professional_id=professional.id,
        name=name,
        birth_date=date(2019, 3, 3),
        diagnosis_keys=[],
        status="ativo",
        start_date=date(2026, 1, 1),
        avatar_color="oklch(0.58 0.12 205)",
    )
    db_session.add(patient)
    await db_session.commit()
    await db_session.refresh(patient)
    return patient


async def _add_attachment(db_session, professional, patient, name="termo.pdf"):
    attachment = Attachment(
        patient_id=patient.id,
        professional_id=professional.id,
        name=name,
        category="documento",
        size_bytes=128,
        storage_key=f"patients/{patient.id}/attachments/{uuid4().hex}",
        date=datetime.now(UTC),
    )
    db_session.add(attachment)
    await db_session.commit()
    await db_session.refresh(attachment)
    return attachment


def _school_authorization(caregiver_id, **overrides):
    authorization = {
        "caregiverId": str(caregiver_id),
        "authorizedAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "evidenceReference": EVIDENCE_TEXT,
        "reviewed": True,
    }
    authorization.update(overrides)
    return authorization


def _school_body(caregiver_id, **overrides):
    body = {
        "channel": "email",
        "recipientKind": "school",
        "email": "escola@example.com",
        "school": {"name": SCHOOL_NAME, "recipientName": SCHOOL_RECIPIENT},
        "schoolAuthorization": _school_authorization(caregiver_id),
    }
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


async def _post_school(api_client, auth_headers, report, body):
    return await api_client.post(
        f"/api/v1/ai/reports/{report.id}/deliveries",
        headers=auth_headers,
        json=body,
    )


async def _stored_delivery(db_session, delivery_id):
    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.id == UUID(str(delivery_id)))
    )
    await db_session.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Happy path — authorization recorded, minimized e-mail, evidence to the owner
# --------------------------------------------------------------------------- #


async def test_school_email_delivery_records_authorization_and_sends_minimized_email(
    api_client,
    auth_headers,
    db_session,
    professional,
    patient,
    escolar_report,
    caregiver,
    send_mock,
):
    caregiver.email = "mae@example.com"
    await db_session.commit()

    response = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["recipientKind"] == "school"
    assert data["status"] == "sent"
    assert data["schoolName"] == SCHOOL_NAME
    assert data["schoolRecipientName"] == SCHOOL_RECIPIENT
    assert data["authorizationRecordedAt"] is not None
    assert data["receivedAt"] is None
    assert data["receivedByName"] is None
    assert data["receivedByRole"] is None
    assert SCHOOL_NAME in data["recipientLabel"]
    assert data["reportVersion"] == 1
    assert data["snapshotMode"] == "fixed"

    # Evidence lives only in the owner's authenticated response.
    authorization = data["schoolAuthorization"]
    assert authorization["caregiverId"] == str(caregiver.id)
    assert authorization["evidenceReference"] == EVIDENCE_TEXT
    assert authorization["evidenceAttachmentId"] is None
    assert authorization["reviewed"] is True
    assert authorization["purpose"] == "school_report"
    assert authorization["recordedAt"] is not None
    assert authorization["recordedByProfessionalId"] == str(professional.id)

    # The school e-mail uses the avulso recipient address, never the caregiver's,
    # and stays free of the patient name / diagnosis in subject and body.
    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_email"] == "escola@example.com"
    assert kwargs["subject"] == "Documento escolar disponível"
    assert patient.name not in kwargs["subject"]
    assert "João" not in kwargs["html"]
    assert "Maria Silva" not in kwargs["html"]
    assert "diagn" not in kwargs["html"].lower()
    assert "/relatorio/" in kwargs["html"]
    assert "/relatorio/" in kwargs["text"]

    stored = await _stored_delivery(db_session, data["id"])
    assert stored.recipient_kind == "school"
    assert stored.school_name == SCHOOL_NAME
    assert stored.school_recipient_name == SCHOOL_RECIPIENT
    assert set(stored.school_authorization) == SCHOOL_AUTHORIZATION_KEYS
    assert stored.school_authorization["caregiverId"] == str(caregiver.id)
    assert stored.school_authorization["evidenceReference"] == EVIDENCE_TEXT
    assert stored.school_authorization["purpose"] == "school_report"
    assert stored.school_authorization["evidenceAttachmentId"] is None
    assert stored.authorization_recorded_at is not None
    assert stored.received_at is None
    assert stored.received_by_name is None
    assert stored.received_by_role is None
    # Restricted JSON: no token, URL or clinical content.
    serialized = repr(stored.school_authorization)
    assert "/relatorio/" not in serialized
    assert "token" not in serialized.lower()
    assert patient.name not in serialized


async def test_school_link_delivery_is_created_without_sending_email(
    api_client, auth_headers, db_session, escolar_report, caregiver, send_mock
):
    response = await _post_school(
        api_client,
        auth_headers,
        escolar_report,
        _school_body(caregiver.id, channel="link", email=None),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "created"
    assert data["recipientKind"] == "school"
    assert data["url"] and "/relatorio/" in data["url"]
    send_mock.assert_not_called()

    token = data["url"].rsplit("/", 1)[-1]
    public = await api_client.get(f"/api/v1/report-deliveries/{token}")
    assert public.status_code == 200, public.text
    public_body = public.json()
    assert public_body["reportType"] == "escolar"
    # The public DTO never carries authorization evidence.
    assert "schoolAuthorization" not in public_body
    assert EVIDENCE_TEXT not in public.text


async def test_listing_deliveries_exposes_school_fields_to_owner(
    api_client, auth_headers, db_session, escolar_report, caregiver, send_mock
):
    created = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert created.status_code == 201, created.text

    listing = await api_client.get(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries", headers=auth_headers
    )
    assert listing.status_code == 200, listing.text
    row = listing.json()[0]
    assert row["recipientKind"] == "school"
    assert row["schoolName"] == SCHOOL_NAME
    assert row["schoolRecipientName"] == SCHOOL_RECIPIENT
    assert row["authorizationRecordedAt"] is not None
    assert row["schoolAuthorization"]["evidenceReference"] == EVIDENCE_TEXT
    assert row["receivedAt"] is None

    # Standard deliveries keep reporting recipientKind=standard.
    standard = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert standard.status_code == 201, standard.text
    assert standard.json()["recipientKind"] == "standard"
    assert standard.json()["schoolName"] is None
    assert standard.json()["schoolAuthorization"] is None


async def test_school_delivery_accepts_attachment_evidence_from_same_patient(
    api_client, auth_headers, db_session, professional, patient, escolar_report, caregiver, send_mock
):
    attachment = await _add_attachment(db_session, professional, patient)
    body = _school_body(
        caregiver.id,
        schoolAuthorization=_school_authorization(
            caregiver.id, evidenceReference=None, evidenceAttachmentId=str(attachment.id)
        ),
    )
    response = await _post_school(api_client, auth_headers, escolar_report, body)
    assert response.status_code == 201, response.text
    stored = await _stored_delivery(db_session, response.json()["id"])
    assert stored.school_authorization["evidenceAttachmentId"] == str(attachment.id)
    assert stored.school_authorization["evidenceReference"] is None
    assert response.json()["schoolAuthorization"]["evidenceAttachmentId"] == str(attachment.id)


# --------------------------------------------------------------------------- #
# Owner / type / status / channel gates
# --------------------------------------------------------------------------- #


async def test_school_delivery_requires_escolar_finalized_report(
    api_client, auth_headers, db_session, professional, patient, caregiver, escolar_report
):
    other_type = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="clinico",
        date=date(2026, 9, 1),
        preview="Clínico",
        content="Texto clínico.",
        status="finalized",
    )
    db_session.add(other_type)
    draft = AIReport(
        professional_id=professional.id,
        patient_id=patient.id,
        type="escolar",
        date=date(2026, 9, 1),
        preview="Rascunho",
        content="Rascunho escolar.",
        status="draft",
    )
    db_session.add(draft)
    await db_session.commit()
    await db_session.refresh(other_type)
    await db_session.refresh(draft)

    not_escolar = await _post_school(
        api_client, auth_headers, other_type, _school_body(caregiver.id)
    )
    assert not_escolar.status_code == 409
    assert "escolar" in not_escolar.json()["detail"]

    draft_report = await _post_school(
        api_client, auth_headers, draft, _school_body(caregiver.id)
    )
    assert draft_report.status_code == 409
    assert "Finalize" in draft_report.json()["detail"]


async def test_school_delivery_requires_patient_owner(
    api_client, auth_headers, db_session, professional, patient, caregiver
):
    foreign_patient = await _add_second_patient(db_session, professional, name="Não é dela")
    report = AIReport(
        professional_id=professional.id,
        patient_id=foreign_patient.id,
        type="escolar",
        date=date(2026, 9, 1),
        preview="Escolar",
        content="Texto escolar.",
        status="finalized",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)

    response = await _post_school(api_client, auth_headers, report, _school_body(caregiver.id))
    assert response.status_code == 404


async def test_school_delivery_rejects_whatsapp_channel(
    api_client, auth_headers, escolar_report, caregiver, send_mock
):
    body = _school_body(caregiver.id, channel="whatsapp", email=None)
    body["caregiverId"] = str(caregiver.id)
    response = await _post_school(api_client, auth_headers, escolar_report, body)
    assert response.status_code == 422
    assert "WhatsApp" in response.json()["detail"]


async def test_school_delivery_rejects_f1_caregiver_id(
    api_client, auth_headers, escolar_report, caregiver
):
    body = _school_body(caregiver.id, caregiverId=str(caregiver.id))
    response = await _post_school(api_client, auth_headers, escolar_report, body)
    assert response.status_code == 422
    assert "caregiverId" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Authorization requirements
# --------------------------------------------------------------------------- #


async def test_school_delivery_requires_school_and_authorization(
    api_client, auth_headers, escolar_report, caregiver
):
    missing_school = _school_body(caregiver.id, school=None)
    response = await _post_school(api_client, auth_headers, escolar_report, missing_school)
    assert response.status_code == 422

    missing_authorization = _school_body(caregiver.id, schoolAuthorization=None)
    response = await _post_school(
        api_client, auth_headers, escolar_report, missing_authorization
    )
    assert response.status_code == 422
    assert "autorização" in response.json()["detail"]


async def test_school_delivery_requires_reviewed_true(
    api_client, auth_headers, escolar_report, caregiver
):
    not_reviewed = _school_body(
        caregiver.id, schoolAuthorization=_school_authorization(caregiver.id, reviewed=False)
    )
    response = await _post_school(api_client, auth_headers, escolar_report, not_reviewed)
    assert response.status_code == 422

    missing = _school_body(caregiver.id)
    missing["schoolAuthorization"].pop("reviewed")
    response = await _post_school(api_client, auth_headers, escolar_report, missing)
    assert response.status_code == 422


async def test_school_delivery_requires_at_least_one_evidence(
    api_client, auth_headers, escolar_report, caregiver
):
    no_evidence = _school_body(
        caregiver.id,
        schoolAuthorization=_school_authorization(
            caregiver.id, evidenceReference=None, evidenceAttachmentId=None
        ),
    )
    response = await _post_school(api_client, auth_headers, escolar_report, no_evidence)
    assert response.status_code == 422
    assert "evidência" in response.json()["detail"]

    blank_reference = _school_body(
        caregiver.id,
        schoolAuthorization=_school_authorization(caregiver.id, evidenceReference="   "),
    )
    response = await _post_school(api_client, auth_headers, escolar_report, blank_reference)
    assert response.status_code == 422
    assert "evidência" in response.json()["detail"]


async def test_school_delivery_rejects_future_authorization_date(
    api_client, auth_headers, escolar_report, caregiver
):
    future = _school_body(
        caregiver.id,
        schoolAuthorization=_school_authorization(
            caregiver.id, authorizedAt=(datetime.now(UTC) + timedelta(days=1)).isoformat()
        ),
    )
    response = await _post_school(api_client, auth_headers, escolar_report, future)
    assert response.status_code == 422
    assert "futuro" in response.json()["detail"]


async def test_school_delivery_rejects_caregiver_and_attachment_outside_patient(
    api_client, auth_headers, db_session, professional, patient, escolar_report, caregiver
):
    other_patient = await _add_second_patient(db_session, professional)
    other_caregiver = Caregiver(
        patient_id=other_patient.id,
        name="Responsável de outro paciente",
        relation="Mãe",
        is_primary=True,
    )
    db_session.add(other_caregiver)
    await db_session.commit()
    await db_session.refresh(other_caregiver)
    other_attachment = await _add_attachment(db_session, professional, other_patient)

    foreign_caregiver = _school_body(other_caregiver.id)
    response = await _post_school(
        api_client, auth_headers, escolar_report, foreign_caregiver
    )
    assert response.status_code == 404
    assert "Responsável" in response.json()["detail"]

    unknown_caregiver = _school_body(uuid4())
    response = await _post_school(
        api_client, auth_headers, escolar_report, unknown_caregiver
    )
    assert response.status_code == 404

    foreign_attachment = _school_body(
        caregiver.id,
        schoolAuthorization=_school_authorization(
            caregiver.id, evidenceReference=None, evidenceAttachmentId=str(other_attachment.id)
        ),
    )
    response = await _post_school(
        api_client, auth_headers, escolar_report, foreign_attachment
    )
    assert response.status_code == 404
    assert "Anexo" in response.json()["detail"]


async def test_standard_delivery_forbids_school_data(
    api_client, auth_headers, escolar_report, caregiver, send_mock
):
    with_school = {"channel": "link", "school": {"name": SCHOOL_NAME, "recipientName": SCHOOL_RECIPIENT}}
    response = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json=with_school,
    )
    assert response.status_code == 422
    assert "school" in response.json()["detail"]

    with_authorization = {"channel": "link", "schoolAuthorization": _school_authorization(caregiver.id)}
    response = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json=with_authorization,
    )
    assert response.status_code == 422

    # Plain F1 deliveries keep working untouched.
    standard = await api_client.post(
        f"/api/v1/ai/reports/{escolar_report.id}/deliveries",
        headers=auth_headers,
        json={"channel": "link"},
    )
    assert standard.status_code == 201, standard.text
    assert standard.json()["recipientKind"] == "standard"


# --------------------------------------------------------------------------- #
# Caregiver/contact minimization
# --------------------------------------------------------------------------- #


async def test_school_delivery_does_not_create_or_reuse_caregiver_contact(
    api_client, auth_headers, db_session, patient, escolar_report, caregiver, send_mock
):
    before = await db_session.scalar(select(func.count()).select_from(Caregiver))
    caregiver.email = "mae@example.com"
    await db_session.commit()

    # Without the avulso school address the request is rejected instead of
    # falling back to the authorizing caregiver's e-mail.
    no_email = _school_body(caregiver.id, email=None)
    response = await _post_school(api_client, auth_headers, escolar_report, no_email)
    assert response.status_code == 422
    send_mock.assert_not_called()

    created = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert created.status_code == 201, created.text
    assert send_mock.call_args.kwargs["to_email"] == "escola@example.com"

    after = await db_session.scalar(select(func.count()).select_from(Caregiver))
    assert after == before


async def test_caregiver_profile_changes_do_not_rewrite_recorded_evidence(
    api_client, auth_headers, db_session, patient, escolar_report, caregiver, send_mock
):
    created = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert created.status_code == 201, created.text
    stored = await _stored_delivery(db_session, created.json()["id"])
    recorded = copy.deepcopy(stored.school_authorization)

    updated = await api_client.patch(
        f"/api/v1/patients/{patient.id}/caregivers/{caregiver.id}",
        headers=auth_headers,
        json={"name": "Maria Souza", "relation": "Tia"},
    )
    assert updated.status_code == 200, updated.text

    reloaded = await _stored_delivery(db_session, created.json()["id"])
    assert reloaded.school_authorization == recorded


# --------------------------------------------------------------------------- #
# Send failure semantics — never a false "sent"
# --------------------------------------------------------------------------- #


async def test_school_email_disabled_is_failed_never_sent(
    api_client, auth_headers, db_session, escolar_report, caregiver, monkeypatch
):
    monkeypatch.setattr(
        "app.services.school_report_delivery_service.send_email",
        MagicMock(return_value=None),
    )
    response = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "failed"
    assert data["lastError"]

    stored = await _stored_delivery(db_session, data["id"])
    assert stored.delivery_status == "failed"
    assert stored.last_error

    # The link itself stays valid and revocable even when the e-mail failed.
    token = data["url"].rsplit("/", 1)[-1]
    assert (await api_client.get(f"/api/v1/report-deliveries/{token}")).status_code == 200


async def test_school_email_error_is_failed_never_sent(
    api_client, auth_headers, db_session, escolar_report, caregiver, monkeypatch
):
    monkeypatch.setattr(
        "app.services.school_report_delivery_service.send_email",
        MagicMock(side_effect=RuntimeError("provider down")),
    )
    response = await _post_school(
        api_client, auth_headers, escolar_report, _school_body(caregiver.id)
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "failed"
    assert data["lastError"]

    stored = await _stored_delivery(db_session, data["id"])
    assert stored.delivery_status == "failed"
    assert stored.delivery_status != "sent"


async def test_school_email_result_is_a_second_transaction_and_crash_keeps_created(
    db_session, professional, patient, escolar_report, caregiver, monkeypatch
):
    """The row is persisted before the provider call; if the process dies before
    the result transaction only ``created`` survives — never a false ``sent``."""
    monkeypatch.setattr(
        "app.services.school_report_delivery_service.send_email",
        MagicMock(return_value="email-msg-1"),
    )
    report_id = escolar_report.id
    caregiver_id = str(caregiver.id)
    body = ReportDeliveryCreate.model_validate(_school_body(caregiver.id))
    delivery, _token = await report_delivery_service.create_report_delivery(
        db_session, professional=professional, report=escolar_report, body=body
    )
    assert delivery.delivery_status == "sent"

    # Simulate the crash before the second transaction (caller commit).
    await db_session.rollback()

    row = await db_session.scalar(
        select(ReportDelivery).where(ReportDelivery.report_id == report_id)
    )
    assert row is not None
    assert row.delivery_status == "created"
    assert row.last_error is None
    assert row.school_authorization is not None
    assert row.school_authorization["caregiverId"] == caregiver_id
    assert row.document_snapshot is not None
