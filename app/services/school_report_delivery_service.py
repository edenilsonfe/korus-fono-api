"""F20 — school report deliveries: authorization rules, restricted record,
public acknowledgement (receipt) and derived revocation.

Only the report/patient owner can create a school delivery, only for a finalized
``escolar`` report and only via e-mail or copied link. The school recipient is the
declared institution/contact; the F1 ``caregiverId`` never becomes the school
contact. The public acknowledgement is idempotent, runs under the delivery row
lock (same order as revocation) and never replaces the first declared identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models.ai import AIReport
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_delivery import (
    DELIVERY_CHANNEL_EMAIL,
    DELIVERY_CHANNEL_WHATSAPP,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_SENT,
    RECIPIENT_KIND_SCHOOL,
    RECIPIENT_KIND_STANDARD,
    ReportDelivery,
)
from app.schemas.report_delivery import (
    ReportDeliveryCreate,
    ReportReceiptCreate,
    ReportReceiptResponse,
    SchoolDeliveryAuthorization,
)
from app.services.email.resend_client import send_email
from app.services.email.templates import school_report_delivery_email

logger = logging.getLogger(__name__)

SCHOOL_REPORT_TYPE = "escolar"
SCHOOL_AUTHORIZATION_PURPOSE = "school_report"
# Small tolerance for client/server clock skew when checking `authorizedAt`.
AUTHORIZED_AT_SKEW = timedelta(minutes=5)
EMAIL_FAILED_MESSAGE = "Falha ao enviar o e-mail."
EMAIL_UNAVAILABLE_MESSAGE = "Envio de e-mail indisponível no momento."
RECEIPT_NOT_REQUIRED_MESSAGE = "Este link não aguarda confirmação de recebimento."
RECEIPT_CONFIRMATION_MESSAGE = "Confirme o recebimento para registrar a confirmação."
RECEIPT_NAME_MESSAGE = "Informe o nome de quem confirma o recebimento."
RECEIPT_ROLE_MESSAGE = "Informe a função de quem confirma o recebimento."


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ValidatedSchoolDelivery:
    """Everything the F1 delivery service needs to persist a school delivery."""

    caregiver: Caregiver
    school_name: str
    school_recipient_name: str
    target_email: str
    authorization: dict


def validate_school_payload(body: ReportDeliveryCreate) -> None:
    """Structural school rules, without DB access.

    For ``standard`` deliveries the school-only fields are forbidden (the F1
    payload keeps working exactly as before). For ``school`` deliveries the
    recipient and the authorization declaration are required and coherent.
    """
    if body.recipient_kind == RECIPIENT_KIND_STANDARD:
        if body.school is not None or body.school_authorization is not None:
            raise _unprocessable(
                "Dados escolares são aceitos apenas em entregas com recipientKind=school."
            )
        return
    if body.channel == DELIVERY_CHANNEL_WHATSAPP:
        raise _unprocessable(
            "Entrega escolar não é enviada por WhatsApp; use e-mail ou link."
        )
    if body.caregiver_id:
        raise _unprocessable(
            "recipientKind=school não usa o caregiverId comum; informe o responsável "
            "que autorizou em schoolAuthorization."
        )
    if body.school is None:
        raise _unprocessable("Informe os dados da escola (school).")
    if not body.school.name.strip():
        raise _unprocessable("Informe o nome da escola.")
    if not body.school.recipient_name.strip():
        raise _unprocessable("Informe o nome do destinatário na escola.")
    if body.school_authorization is None:
        raise _unprocessable("Informe a autorização escolar (schoolAuthorization).")
    authorization = body.school_authorization
    if authorization.reviewed is not True:
        raise _unprocessable("Confirme a revisão da autorização escolar (reviewed=true).")
    reference = (authorization.evidence_reference or "").strip()
    if not reference and authorization.evidence_attachment_id is None:
        raise _unprocessable(
            "Informe ao menos uma evidência da autorização "
            "(evidenceReference ou evidenceAttachmentId)."
        )
    if _as_utc(authorization.authorized_at) > datetime.now(UTC) + AUTHORIZED_AT_SKEW:
        raise _unprocessable("A data da autorização não pode estar no futuro.")
    if body.channel == DELIVERY_CHANNEL_EMAIL and not str(body.email or "").strip():
        raise _unprocessable("Informe o e-mail do destinatário na escola.")


def build_school_authorization_record(
    authorization: SchoolDeliveryAuthorization, *, caregiver_id: str
) -> dict:
    """Restricted JSON stored with the delivery: declaring actor, declared date,
    evidence and purpose. Never a token, URL, patient content or answers."""
    reference = (authorization.evidence_reference or "").strip()
    return {
        "caregiverId": caregiver_id,
        "authorizedAt": _as_utc(authorization.authorized_at).isoformat(),
        "evidenceReference": reference or None,
        "evidenceAttachmentId": (
            str(authorization.evidence_attachment_id)
            if authorization.evidence_attachment_id is not None
            else None
        ),
        "reviewed": True,
        "purpose": SCHOOL_AUTHORIZATION_PURPOSE,
    }


async def validate_school_delivery(
    db: AsyncSession,
    *,
    professional: Professional,
    report: AIReport,
    patient: Patient,
    body: ReportDeliveryCreate,
) -> ValidatedSchoolDelivery:
    """Authorize and validate a school delivery before anything is persisted.

    Owner only (404 outside scope), escolar + finalized report (409 otherwise),
    caregiver/attachment scoped to the patient (404). Raises pt-BR HTTPException.
    """
    validate_school_payload(body)
    if patient.professional_id != professional.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado"
        )
    if report.type != SCHOOL_REPORT_TYPE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Somente relatório do tipo escolar pode ser entregue à escola.",
        )
    authorization = body.school_authorization
    caregiver = await db.scalar(
        select(Caregiver).where(
            Caregiver.id == authorization.caregiver_id,
            Caregiver.patient_id == patient.id,
        )
    )
    if caregiver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Responsável não encontrado"
        )
    if authorization.evidence_attachment_id is not None:
        attachment = await db.scalar(
            select(Attachment).where(
                Attachment.id == authorization.evidence_attachment_id,
                Attachment.patient_id == patient.id,
            )
        )
        if attachment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Anexo não encontrado"
            )
    target_email = ""
    if body.channel == DELIVERY_CHANNEL_EMAIL:
        target_email = str(body.email or "").strip()
    return ValidatedSchoolDelivery(
        caregiver=caregiver,
        school_name=body.school.name.strip(),
        school_recipient_name=body.school.recipient_name.strip(),
        target_email=target_email,
        authorization=build_school_authorization_record(
            authorization, caregiver_id=str(caregiver.id)
        ),
    )


async def send_school_delivery_email(
    *,
    professional: Professional,
    school_name: str,
    school_recipient_name: str,
    target_email: str,
    delivery_url: str,
    expires_days: int,
    delivery: ReportDelivery,
) -> None:
    """Send the minimized school notice and record the outcome on the delivery.

    Never marks ``sent`` unless the provider confirmed acceptance: a disabled or
    failed send leaves ``failed`` + ``lastError`` for the professional.
    """
    rendered = school_report_delivery_email(
        professional_name=professional.name,
        school_name=school_name,
        school_recipient_name=school_recipient_name,
        delivery_url=delivery_url,
        expires_days=expires_days,
    )
    try:
        message_id = await run_in_threadpool(
            send_email,
            to_email=target_email,
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
        )
    except Exception:  # noqa: BLE001 - the row records the failure for the owner
        logger.exception("Failed to send school report delivery e-mail")
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        delivery.last_error = EMAIL_FAILED_MESSAGE
        return
    if message_id is None:
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        delivery.last_error = EMAIL_UNAVAILABLE_MESSAGE
        return
    delivery.delivery_status = DELIVERY_STATUS_SENT


async def revoke_school_deliveries_for_caregiver(
    db: AsyncSession, *, patient_id: UUID, caregiver_id: UUID
) -> int:
    """Revoke the school deliveries authorized by this caregiver, before the
    reference disappears. The stored authorization snapshot is preserved: a later
    removal cannot resurrect the link and receipt history stays readable."""
    rows = await db.scalars(
        select(ReportDelivery).where(
            ReportDelivery.patient_id == patient_id,
            ReportDelivery.recipient_kind == RECIPIENT_KIND_SCHOOL,
            ReportDelivery.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    revoked = 0
    for delivery in rows:
        authorization = delivery.school_authorization or {}
        if str(authorization.get("caregiverId") or "") != str(caregiver_id):
            continue
        delivery.revoked_at = now
        revoked += 1
    if revoked:
        await db.flush()
    return revoked


def validate_receipt_payload(body: ReportReceiptCreate) -> None:
    """Structural rules for the public acknowledgement payload (pt-BR 422).

    The client only declares who is confirming; the timestamp is always the
    server's. Whitespace-only identities are rejected here (Pydantic covers
    type/length).
    """
    if body.received is not True:
        raise _unprocessable(RECEIPT_CONFIRMATION_MESSAGE)
    if not body.receiver_name.strip():
        raise _unprocessable(RECEIPT_NAME_MESSAGE)
    if not body.receiver_role.strip():
        raise _unprocessable(RECEIPT_ROLE_MESSAGE)


async def acknowledge_school_delivery(
    db: AsyncSession,
    *,
    token: str,
    body: ReportReceiptCreate,
) -> ReportReceiptResponse:
    """Public, idempotent acknowledgement of a school delivery (F20, fase 2).

    Locks the delivery row — the same single lock order used by revocation, so
    the two never interleave halfway. Validity mirrors the public GET (unknown/
    revoked/expired token or deactivated account -> 410; non-school delivery ->
    409). The first confirmation writes the self-declared identity and the
    server timestamp; replays return the stored receipt without replacing that
    identity. The caller commits before answering, so a 200 never precedes the
    persisted receipt.
    """
    # Local import: report_delivery_service imports this module at import time.
    from app.services.report_delivery_service import (
        INVALID_DELIVERY_MESSAGE,
        REVOKED_DELIVERY_MESSAGE,
        InvalidReportDeliveryToken,
        delivery_content_hash,
        delivery_report_version,
        hash_delivery_token,
    )

    validate_receipt_payload(body)
    raw = (token or "").strip()
    if not raw or len(raw) > 128:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    delivery = await db.scalar(
        select(ReportDelivery)
        .where(ReportDelivery.token_hash == hash_delivery_token(raw))
        .with_for_update()
    )
    if delivery is None:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    if delivery.revoked_at is not None:
        raise InvalidReportDeliveryToken(REVOKED_DELIVERY_MESSAGE)
    expires_at = _as_utc(delivery.expires_at)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    professional = await db.get(Professional, delivery.professional_id)
    if professional is None or professional.is_disabled:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    if (delivery.recipient_kind or RECIPIENT_KIND_STANDARD) != RECIPIENT_KIND_SCHOOL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=RECEIPT_NOT_REQUIRED_MESSAGE,
        )
    if delivery.received_at is None:
        delivery.received_at = datetime.now(UTC)
        delivery.received_by_name = body.receiver_name.strip()
        delivery.received_by_role = body.receiver_role.strip()
        await db.flush()
    return ReportReceiptResponse(
        # Normalized to UTC so the receipt reads the same before and after a
        # database round-trip (SQLite drops tzinfo; PostgreSQL keeps it).
        received_at=_as_utc(delivery.received_at),
        report_version=delivery_report_version(delivery),
        content_hash=delivery_content_hash(delivery),
    )
