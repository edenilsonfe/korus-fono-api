"""Delivery of finalized AI reports: revocable public links plus WhatsApp/e-mail dispatch."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.models.ai import AIReport
from app.models.caregiver import Caregiver
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.report_delivery import (
    DELIVERY_CHANNEL_EMAIL,
    DELIVERY_CHANNEL_WHATSAPP,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_SENT,
    ReportDelivery,
)
from app.schemas.report_delivery import ReportDeliveryCreate
from app.services.email.resend_client import send_email
from app.services.email.templates import report_delivery_email
from app.services.evolution_whatsapp_service import mask_phone
from app.services.report_export import REPORT_TYPE_LABELS
from app.services.whatsapp_provider import get_active_whatsapp_provider

logger = logging.getLogger(__name__)

PUBLIC_PATH_PREFIX = "/relatorio"
INVALID_DELIVERY_MESSAGE = "Link inválido ou expirado."
REVOKED_DELIVERY_MESSAGE = "Este link foi revogado pelo profissional."


class InvalidReportDeliveryToken(ValueError):
    """Raw token unknown, revoked, expired or missing its records."""


@dataclass(frozen=True)
class PublicDeliveryContext:
    delivery: ReportDelivery
    report: AIReport
    professional: Professional
    patient: Patient


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_delivery_url(token: str) -> str:
    base = (get_settings().frontend_url or "").strip().rstrip("/")
    return f"{base}{PUBLIC_PATH_PREFIX}/{token}"


def _whatsapp_message(
    *,
    professional: Professional,
    patient: Patient,
    report: AIReport,
    url: str,
    expires_days: int,
) -> str:
    label = REPORT_TYPE_LABELS.get(report.type, report.type)
    first_name = (patient.name or "").strip().split(" ")[0] or patient.name
    return (
        f"Olá! Aqui é {professional.name}. Envio o documento \"{label}\" de {first_name}, "
        f"preparado(a) por mim no Korus Fono.\n\n"
        f"Acesse pelo link (válido por {expires_days} dias):\n{url}"
    )


async def _send_whatsapp(
    db: AsyncSession,
    *,
    professional: Professional,
    patient: Patient,
    caregiver: Caregiver,
    report: AIReport,
    url: str,
    expires_days: int,
    delivery: ReportDelivery,
) -> None:
    provider = get_active_whatsapp_provider(db)
    text = _whatsapp_message(
        professional=professional,
        patient=patient,
        report=report,
        url=url,
        expires_days=expires_days,
    )
    phone = (caregiver.phone or "").strip()
    try:
        result = await provider.send_text_message(professional.id, phone, text)
    except HTTPException as exc:
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        detail = str(getattr(exc, "detail", "") or "Falha ao enviar a mensagem.")
        delivery.last_error = detail[:255]
        return
    except Exception:  # noqa: BLE001 - provider may fail after accepting the message
        logger.exception("Failed to deliver report by WhatsApp")
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        delivery.last_error = "Entrega incerta; envio não repetido automaticamente."
        return
    delivery.delivery_status = DELIVERY_STATUS_SENT
    delivery.provider_message_id = result.provider_message_id


async def _send_email(
    *,
    professional: Professional,
    patient: Patient,
    report: AIReport,
    url: str,
    expires_days: int,
    target_email: str,
    delivery: ReportDelivery,
) -> None:
    rendered = report_delivery_email(
        professional_name=professional.name,
        patient_name=patient.name,
        report_label=REPORT_TYPE_LABELS.get(report.type, report.type),
        delivery_url=url,
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
    except Exception:  # noqa: BLE001 - delivery row records the failure for the professional
        logger.exception("Failed to deliver report by e-mail")
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        delivery.last_error = "Falha ao enviar o e-mail."
        return
    if message_id is None:
        delivery.delivery_status = DELIVERY_STATUS_FAILED
        delivery.last_error = "Envio de e-mail indisponível no momento."
        return
    delivery.delivery_status = DELIVERY_STATUS_SENT


async def create_report_delivery(
    db: AsyncSession,
    *,
    professional: Professional,
    report: AIReport,
    body: ReportDeliveryCreate,
) -> tuple[ReportDelivery, str]:
    """Create the delivery row, dispatch it when needed and return (row, raw token)."""
    if report.status != "finalized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finalize o relatório antes de entregar.",
        )
    patient = await db.get(Patient, report.patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado")

    caregiver: Caregiver | None = None
    if body.caregiver_id and body.channel in (DELIVERY_CHANNEL_WHATSAPP, DELIVERY_CHANNEL_EMAIL):
        try:
            caregiver_uuid = UUID(str(body.caregiver_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Responsável inválido.",
            ) from exc
        caregiver = await db.scalar(
            select(Caregiver).where(
                Caregiver.id == caregiver_uuid,
                Caregiver.patient_id == patient.id,
            )
        )
        if caregiver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Responsável não encontrado",
            )

    recipient_label = "Link avulso"
    target_email = ""
    if body.channel == DELIVERY_CHANNEL_WHATSAPP:
        if caregiver is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selecione o responsável que vai receber a mensagem.",
            )
        if not caregiver.whatsapp_opt_in:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Responsável sem autorização para receber mensagens por WhatsApp.",
            )
        phone = (caregiver.phone or "").strip()
        if not phone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Responsável sem telefone cadastrado.",
            )
        provider = get_active_whatsapp_provider(db)
        if not await provider.can_send(professional.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conecte o WhatsApp para enviar por mensagem.",
            )
        recipient_label = f"{caregiver.name} · {mask_phone(phone)}"
    elif body.channel == DELIVERY_CHANNEL_EMAIL:
        target_email = (str(body.email).strip() if body.email else "") or (
            caregiver.email.strip() if caregiver else ""
        )
        if not target_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Informe o e-mail do destinatário.",
            )
        recipient_label = target_email

    token = secrets.token_urlsafe(32)
    delivery = ReportDelivery(
        report_id=report.id,
        professional_id=professional.id,
        patient_id=patient.id,
        channel=body.channel,
        recipient_label=recipient_label,
        token_hash=_hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_in_days),
    )
    db.add(delivery)
    await db.flush()

    url = build_delivery_url(token)
    if body.channel == DELIVERY_CHANNEL_WHATSAPP and caregiver is not None:
        await _send_whatsapp(
            db,
            professional=professional,
            patient=patient,
            caregiver=caregiver,
            report=report,
            url=url,
            expires_days=body.expires_in_days,
            delivery=delivery,
        )
    elif body.channel == DELIVERY_CHANNEL_EMAIL:
        await _send_email(
            professional=professional,
            patient=patient,
            report=report,
            url=url,
            expires_days=body.expires_in_days,
            target_email=target_email,
            delivery=delivery,
        )
    return delivery, token


async def list_report_deliveries(db: AsyncSession, *, report_id: UUID) -> list[ReportDelivery]:
    rows = await db.scalars(
        select(ReportDelivery)
        .where(ReportDelivery.report_id == report_id)
        .order_by(ReportDelivery.created_at.desc(), ReportDelivery.id.desc())
    )
    return list(rows)


async def revoke_report_delivery(
    db: AsyncSession, *, report_id: UUID, delivery_id: UUID
) -> ReportDelivery:
    delivery = await db.scalar(
        select(ReportDelivery).where(
            ReportDelivery.id == delivery_id,
            ReportDelivery.report_id == report_id,
        )
    )
    if delivery is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Entrega não encontrada"
        )
    if delivery.revoked_at is None:
        delivery.revoked_at = datetime.now(UTC)
    await db.flush()
    return delivery


async def load_public_delivery(db: AsyncSession, token: str) -> PublicDeliveryContext:
    raw = (token or "").strip()
    if not raw or len(raw) > 128:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    delivery = await db.scalar(
        select(ReportDelivery).where(ReportDelivery.token_hash == _hash_token(raw))
    )
    if delivery is None:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    if delivery.revoked_at is not None:
        raise InvalidReportDeliveryToken(REVOKED_DELIVERY_MESSAGE)
    expires_at = _as_utc(delivery.expires_at)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    report = await db.get(AIReport, delivery.report_id)
    professional = await db.get(Professional, delivery.professional_id)
    patient = await db.get(Patient, delivery.patient_id)
    if report is None or professional is None or patient is None:
        raise InvalidReportDeliveryToken(INVALID_DELIVERY_MESSAGE)
    return PublicDeliveryContext(
        delivery=delivery, report=report, professional=professional, patient=patient
    )


async def register_delivery_view(db: AsyncSession, delivery: ReportDelivery) -> None:
    now = datetime.now(UTC)
    delivery.view_count = (delivery.view_count or 0) + 1
    if delivery.first_viewed_at is None:
        delivery.first_viewed_at = now
    delivery.last_viewed_at = now
    await db.commit()


async def register_delivery_download(db: AsyncSession, delivery: ReportDelivery) -> None:
    now = datetime.now(UTC)
    delivery.download_count = (delivery.download_count or 0) + 1
    delivery.last_downloaded_at = now
    await db.commit()
