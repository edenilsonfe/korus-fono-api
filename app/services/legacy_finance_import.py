"""Conservative financial import for a supported legacy clinic export."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.finance import (
    FinancialAuditEvent,
    FinancialPayment,
    PaymentAllocation,
    PaymentMethod,
    Receivable,
    ReceivableItem,
)
from app.services.legacy_clinic_import import (
    LegacyClinicImportError,
    _caregiver_specs,
    _existing_ids,
    _import_namespace,
    _is_true,
    _money_to_cents,
    _parse_legacy_sql,
    _parse_datetime,
    _payload_sha256,
    _pending_manifest_path,
    _read_manifest,
    _resolve_professional,
    _target_uuid,
    _write_manifest,
)


_PAYMENT_METHOD_NAME = "Não informado — migração"
_MANIFEST_SCHEMA_VERSION = 1
_SOURCE_SYSTEM = "legacy_clinic_finance"
_REVIEW_HEADERS = (
    "Data do atendimento",
    "Paciente",
    "Responsável",
    "Serviço",
    "Valor (R$)",
    "Vinculado a pacote na origem",
    "Status na origem",
    "Cobrar? (Sim/Não)",
    "Observação da profissional",
)


@dataclass(frozen=True)
class LegacyFinanceImportPreview:
    professional_id: UUID
    source_sha256: str
    manifest_path: str
    review_csv_path: str
    paid_records: int
    paid_total_cents: int
    review_records: int
    review_total_cents: int
    review_package_records: int
    skipped_paid_zero_records: int
    skipped_open_zero_records: int
    warnings: list[str]


@dataclass(frozen=True)
class LegacyFinanceImportResult:
    professional_id: UUID
    source_sha256: str
    manifest_path: str
    review_csv_path: str
    created_counts: dict[str, int]
    skipped_counts: dict[str, int]
    verified_counts: dict[str, int]
    paid_total_cents: int
    review_records: int
    review_total_cents: int


@dataclass(frozen=True)
class _FinancePlan:
    paid: list[dict[str, str | None]]
    review: list[dict[str, str | None]]
    paid_zero: list[dict[str, str | None]]
    open_zero: list[dict[str, str | None]]


def default_legacy_finance_manifest_path(source_path: Path) -> Path:
    path = Path(source_path)
    return path.with_name(f"{path.name}.korus-finance-import.json")


def default_legacy_finance_review_csv_path(source_path: Path) -> Path:
    path = Path(source_path)
    return path.with_name(f"{path.name}.korus-finance-review.csv")


def _finance_plan(rows: dict[str, list[dict[str, str | None]]]) -> _FinancePlan:
    paid: list[dict[str, str | None]] = []
    review: list[dict[str, str | None]] = []
    paid_zero: list[dict[str, str | None]] = []
    open_zero: list[dict[str, str | None]] = []

    for appointment in rows["agendamento"]:
        if appointment.get("estado_agenda_id") != "4" or not _is_true(
            appointment.get("ativo")
        ):
            continue
        source_id = str(appointment["id"])
        total_cents = _money_to_cents(
            appointment.get("valor_total"), f"agendamento/{source_id}.valor_total"
        )
        paid_cents = _money_to_cents(
            appointment.get("valor_pago"), f"agendamento/{source_id}.valor_pago"
        )
        if total_cents is None or paid_cents is None:
            raise LegacyClinicImportError(
                f"Atendimento concluído sem valores financeiros completos: {source_id}"
            )

        status = (appointment.get("status_pagamento") or "").strip()
        if status == "Pago":
            if total_cents == 0 and paid_cents == 0:
                paid_zero.append(appointment)
                continue
            if (
                total_cents <= 0
                or paid_cents <= 0
                or total_cents != paid_cents
                or not _is_true(appointment.get("faturado"))
            ):
                raise LegacyClinicImportError(
                    f"Pagamento inconsistente no atendimento de origem {source_id}"
                )
            if appointment.get("pacote_id") not in (None, ""):
                raise LegacyClinicImportError(
                    f"Pagamento ligado a pacote sem definição importável: {source_id}"
                )
            paid.append(appointment)
            continue

        if status == "Em aberto":
            if paid_cents != 0:
                raise LegacyClinicImportError(
                    f"Atendimento em aberto possui valor pago na origem: {source_id}"
                )
            if _is_true(appointment.get("faturado")):
                raise LegacyClinicImportError(
                    f"Atendimento em aberto consta como faturado na origem: {source_id}"
                )
            if total_cents > 0:
                review.append(appointment)
            elif total_cents == 0:
                open_zero.append(appointment)
            else:
                raise LegacyClinicImportError(
                    f"Atendimento em aberto possui valor negativo: {source_id}"
                )
            continue

        raise LegacyClinicImportError(
            f"Status financeiro não reconhecido no atendimento concluído {source_id}: {status or '<vazio>'}"
        )

    return _FinancePlan(
        paid=paid,
        review=review,
        paid_zero=paid_zero,
        open_zero=open_zero,
    )


async def _validate_clinical_prerequisites(
    db: AsyncSession,
    *,
    professional_id: UUID,
    namespace: UUID,
    plan: _FinancePlan,
) -> dict[UUID, Appointment]:
    source_appointments = plan.paid + plan.review
    target_ids = {
        _target_uuid(namespace, "appointment", str(source["id"]))
        for source in source_appointments
    }
    if not target_ids:
        return {}
    result = await db.execute(
        select(Appointment).where(
            Appointment.id.in_(target_ids),
            Appointment.professional_id == professional_id,
        )
    )
    targets = {appointment.id: appointment for appointment in result.scalars().all()}
    if set(targets) != target_ids:
        raise LegacyClinicImportError(
            "A importação clínica precisa estar completa antes da etapa financeira"
        )
    if any(appointment.status != "concluido" for appointment in targets.values()):
        raise LegacyClinicImportError(
            "Há atendimento financeiro cujo destino não está concluído"
        )
    return targets


def _payer_details(patient: dict[str, str | None]) -> tuple[str, str]:
    caregivers = _caregiver_specs(patient)
    payer_name = str(caregivers[0]["name"]) if caregivers else str(patient.get("nome") or "")
    source_candidates = (
        (patient.get("responsavel_nome"), patient.get("responsavel_cpf")),
        (patient.get("responsavel_nome_2"), patient.get("responsavel_cpf_2")),
        (patient.get("mae_nome"), patient.get("mae_cpf")),
        (patient.get("pai_nome"), patient.get("pai_cpf")),
    )
    payer_document = ""
    if caregivers:
        payer_key = " ".join(payer_name.split()).casefold()
        for candidate_name, candidate_document in source_candidates:
            if " ".join((candidate_name or "").split()).casefold() == payer_key:
                payer_document = re.sub(r"\D", "", candidate_document or "")[:20]
                break
    return payer_name[:180], payer_document


def _money_for_csv(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d}"


def _review_csv_bytes(
    *,
    plan: _FinancePlan,
    patients_by_source: dict[str, dict[str, str | None]],
    procedures_by_source: dict[str, dict[str, str | None]],
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(_REVIEW_HEADERS),
        delimiter=";",
        lineterminator="\n",
    )
    writer.writeheader()
    ordered_review = sorted(
        plan.review,
        key=lambda row: (
            _parse_datetime(row.get("dt_inicial"), "agendamento.dt_inicial"),
            str(row["id"]),
        ),
    )
    for appointment in ordered_review:
        patient = patients_by_source[str(appointment["paciente_id"])]
        procedure = procedures_by_source.get(str(appointment.get("procedimento_id")))
        payer_name, _ = _payer_details(patient)
        amount_cents = _money_to_cents(
            appointment.get("valor_total"), "agendamento.valor_total"
        )
        if amount_cents is None:
            raise LegacyClinicImportError("Valor ausente ao gerar a planilha de conferência")
        starts_at = _parse_datetime(
            appointment.get("dt_inicial"), "agendamento.dt_inicial"
        )
        writer.writerow(
            {
                "Data do atendimento": starts_at.strftime("%d/%m/%Y"),
                "Paciente": " ".join((patient.get("nome") or "").split()),
                "Responsável": payer_name,
                "Serviço": (
                    " ".join((procedure.get("nome") or "").split())
                    if procedure
                    else "Atendimento importado"
                ),
                "Valor (R$)": _money_for_csv(amount_cents),
                "Vinculado a pacote na origem": (
                    "SIM — REVISAR PACOTE"
                    if appointment.get("pacote_id") not in (None, "")
                    else "Não"
                ),
                "Status na origem": "Em aberto",
                "Cobrar? (Sim/Não)": "",
                "Observação da profissional": "",
            }
        )
    return buffer.getvalue().encode("utf-8-sig")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes(payload)
        temporary_path.replace(path)
    except OSError as exc:
        raise LegacyClinicImportError(
            "Não foi possível gravar a planilha de conferência"
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def _finance_target_ids(namespace: UUID, source_id: str) -> dict[str, UUID]:
    return {
        "receivables": _target_uuid(namespace, "finance-receivable", source_id),
        "receivable_items": _target_uuid(namespace, "finance-receivable-item", source_id),
        "payments": _target_uuid(namespace, "finance-payment", source_id),
        "allocations": _target_uuid(namespace, "finance-payment-allocation", source_id),
        "receivable_audit": _target_uuid(namespace, "finance-receivable-audit", source_id),
        "payment_audit": _target_uuid(namespace, "finance-payment-audit", source_id),
    }


def _finance_manifest_payload(
    *,
    professional_id: UUID,
    source_sha256: str,
    method_id: UUID,
    plan: _FinancePlan,
    namespace: UUID,
    review_csv_sha256: str,
    verified_counts: dict[str, int],
) -> dict:
    records = []
    for source in sorted(plan.paid, key=lambda row: str(row["id"])):
        source_id = str(source["id"])
        ids = _finance_target_ids(namespace, source_id)
        records.append(
            {
                "payloadSha256": _payload_sha256(source),
                "sourceAppointmentId": source_id,
                "targetIds": {key: str(value) for key, value in sorted(ids.items())},
            }
        )
    return {
        "schemaVersion": _MANIFEST_SCHEMA_VERSION,
        "sourceSystem": _SOURCE_SYSTEM,
        "professionalId": str(professional_id),
        "sourceFileSha256": source_sha256,
        "paymentMethodId": str(method_id),
        "records": records,
        "reviewCsvInitialSha256": review_csv_sha256,
        "verifiedCounts": verified_counts,
    }


def _validate_finance_manifest(existing: dict, expected: dict) -> None:
    if existing != expected:
        raise LegacyClinicImportError(
            "O manifesto financeiro local não corresponde à origem ou aos destinos esperados"
        )


async def _presence_counts(
    db: AsyncSession,
    *,
    method_id: UUID,
    target_ids: dict[str, list[UUID]],
) -> dict[str, int]:
    return {
        "allocations": len(
            await _existing_ids(db, PaymentAllocation, target_ids["allocations"])
        ),
        "audit_events": len(
            await _existing_ids(db, FinancialAuditEvent, target_ids["audit_events"])
        ),
        "payment_methods": len(await _existing_ids(db, PaymentMethod, [method_id])),
        "payments": len(await _existing_ids(db, FinancialPayment, target_ids["payments"])),
        "receivable_items": len(
            await _existing_ids(db, ReceivableItem, target_ids["receivable_items"])
        ),
        "receivables": len(
            await _existing_ids(db, Receivable, target_ids["receivables"])
        ),
    }


async def preview_legacy_finance_import(
    db: AsyncSession,
    *,
    source_path: Path,
    professional_email: str,
    manifest_path: Path | None = None,
    review_csv_path: Path | None = None,
) -> LegacyFinanceImportPreview:
    """Classify safe paid history and review candidates without writing."""

    source_path = Path(source_path)
    professional = await _resolve_professional(db, professional_email)
    source_sha256, rows = _parse_legacy_sql(source_path)
    namespace = _import_namespace(professional.id, rows)
    plan = _finance_plan(rows)
    await _validate_clinical_prerequisites(
        db,
        professional_id=professional.id,
        namespace=namespace,
        plan=plan,
    )

    paid_total_cents = sum(
        _money_to_cents(row.get("valor_total"), "agendamento.valor_total") or 0
        for row in plan.paid
    )
    review_total_cents = sum(
        _money_to_cents(row.get("valor_total"), "agendamento.valor_total") or 0
        for row in plan.review
    )
    review_package_records = sum(
        row.get("pacote_id") not in (None, "") for row in plan.review
    )
    warnings: list[str] = []
    if plan.paid_zero:
        warnings.append(
            f"{len(plan.paid_zero)} atendimento marcado como pago tem valor zero e não gerará lançamento financeiro."
            if len(plan.paid_zero) == 1
            else f"{len(plan.paid_zero)} atendimentos marcados como pagos têm valor zero e não gerarão lançamentos financeiros."
        )
    if plan.review:
        warnings.append(
            f"{len(plan.review)} atendimento em aberto será enviado apenas para a planilha de conferência."
            if len(plan.review) == 1
            else f"{len(plan.review)} atendimentos em aberto serão enviados apenas para a planilha de conferência."
        )
    if review_package_records:
        warnings.append(
            f"{review_package_records} item da conferência está ligado a pacote na origem e requer atenção especial."
            if review_package_records == 1
            else f"{review_package_records} itens da conferência estão ligados a pacote na origem e requerem atenção especial."
        )
    if plan.open_zero:
        warnings.append(
            f"{len(plan.open_zero)} atendimento em aberto tem valor zero e ficou fora da planilha."
            if len(plan.open_zero) == 1
            else f"{len(plan.open_zero)} atendimentos em aberto têm valor zero e ficaram fora da planilha."
        )

    return LegacyFinanceImportPreview(
        professional_id=professional.id,
        source_sha256=source_sha256,
        manifest_path=str(
            Path(manifest_path or default_legacy_finance_manifest_path(source_path))
        ),
        review_csv_path=str(
            Path(review_csv_path or default_legacy_finance_review_csv_path(source_path))
        ),
        paid_records=len(plan.paid),
        paid_total_cents=paid_total_cents,
        review_records=len(plan.review),
        review_total_cents=review_total_cents,
        review_package_records=review_package_records,
        skipped_paid_zero_records=len(plan.paid_zero),
        skipped_open_zero_records=len(plan.open_zero),
        warnings=warnings,
    )


async def apply_legacy_finance_import(
    db: AsyncSession,
    *,
    source_path: Path,
    professional_email: str,
    expected_source_sha256: str | None = None,
    manifest_path: Path | None = None,
    review_csv_path: Path | None = None,
) -> LegacyFinanceImportResult:
    """Import only fully proven payments and emit open items for human review."""

    source_path = Path(source_path)
    professional = await _resolve_professional(db, professional_email)
    source_sha256, rows = _parse_legacy_sql(source_path)
    if not expected_source_sha256:
        raise LegacyClinicImportError(
            "Informe o SHA-256 exibido no dry-run financeiro revisado"
        )
    if expected_source_sha256.strip().lower() != source_sha256:
        raise LegacyClinicImportError(
            "O SHA-256 do arquivo não corresponde ao dry-run financeiro revisado"
        )

    resolved_manifest_path = Path(
        manifest_path or default_legacy_finance_manifest_path(source_path)
    )
    resolved_review_path = Path(
        review_csv_path or default_legacy_finance_review_csv_path(source_path)
    )
    resolved_paths = {
        source_path.resolve(),
        resolved_manifest_path.resolve(),
        resolved_review_path.resolve(),
    }
    if len(resolved_paths) != 3:
        raise LegacyClinicImportError(
            "Origem, manifesto financeiro e planilha de conferência devem ser arquivos distintos"
        )

    namespace = _import_namespace(professional.id, rows)
    plan = _finance_plan(rows)
    target_appointments = await _validate_clinical_prerequisites(
        db,
        professional_id=professional.id,
        namespace=namespace,
        plan=plan,
    )
    patients_by_source = {str(row["id"]): row for row in rows["pacientes"]}
    procedures_by_source = {str(row["id"]): row for row in rows["procedimento"]}
    if any(
        str(appointment.get("paciente_id")) not in patients_by_source
        for appointment in plan.paid + plan.review
    ):
        raise LegacyClinicImportError(
            "Há atendimento financeiro apontando para paciente ausente no arquivo"
        )

    review_payload = _review_csv_bytes(
        plan=plan,
        patients_by_source=patients_by_source,
        procedures_by_source=procedures_by_source,
    )
    review_sha256 = hashlib.sha256(review_payload).hexdigest()
    paid_total_cents = sum(
        _money_to_cents(row.get("valor_total"), "agendamento.valor_total") or 0
        for row in plan.paid
    )
    review_total_cents = sum(
        _money_to_cents(row.get("valor_total"), "agendamento.valor_total") or 0
        for row in plan.review
    )

    method = await db.scalar(
        select(PaymentMethod).where(
            PaymentMethod.professional_id == professional.id,
            PaymentMethod.name == _PAYMENT_METHOD_NAME,
        )
    )
    method_id = (
        method.id
        if method is not None
        else _target_uuid(namespace, "finance-payment-method", "unknown-migration")
    )
    all_ids = [_finance_target_ids(namespace, str(row["id"])) for row in plan.paid]
    target_ids = {
        "allocations": [ids["allocations"] for ids in all_ids],
        "audit_events": [
            audit_id
            for ids in all_ids
            for audit_id in (ids["receivable_audit"], ids["payment_audit"])
        ],
        "payments": [ids["payments"] for ids in all_ids],
        "receivable_items": [ids["receivable_items"] for ids in all_ids],
        "receivables": [ids["receivables"] for ids in all_ids],
    }
    expected_counts = {
        "allocations": len(plan.paid),
        "audit_events": len(plan.paid) * 2,
        "payment_methods": 1,
        "payments": len(plan.paid),
        "receivable_items": len(plan.paid),
        "receivables": len(plan.paid),
    }
    empty_counts = {key: 0 for key in expected_counts}
    expected_manifest = _finance_manifest_payload(
        professional_id=professional.id,
        source_sha256=source_sha256,
        method_id=method_id,
        plan=plan,
        namespace=namespace,
        review_csv_sha256=review_sha256,
        verified_counts=expected_counts,
    )
    pending_manifest_path = _pending_manifest_path(resolved_manifest_path)
    pending_review_path = resolved_review_path.with_name(
        f"{resolved_review_path.name}.pending"
    )
    manifest = _read_manifest(resolved_manifest_path)
    pending_manifest = _read_manifest(pending_manifest_path)
    if manifest is not None and pending_manifest is not None:
        raise LegacyClinicImportError(
            "Há um manifesto financeiro pendente junto de uma importação concluída"
        )

    presence = await _presence_counts(
        db,
        method_id=method_id,
        target_ids=target_ids,
    )
    financial_presence = {
        key: value for key, value in presence.items() if key != "payment_methods"
    }
    if manifest is not None:
        _validate_finance_manifest(manifest, expected_manifest)
        if presence != expected_counts:
            raise LegacyClinicImportError(
                "Um ou mais destinos financeiros rastreados no manifesto não existem mais"
            )
        if not resolved_review_path.exists():
            _write_bytes_atomic(resolved_review_path, review_payload)
        return LegacyFinanceImportResult(
            professional_id=professional.id,
            source_sha256=source_sha256,
            manifest_path=str(resolved_manifest_path),
            review_csv_path=str(resolved_review_path),
            created_counts=empty_counts,
            skipped_counts=expected_counts,
            verified_counts=expected_counts,
            paid_total_cents=paid_total_cents,
            review_records=len(plan.review),
            review_total_cents=review_total_cents,
        )

    if pending_manifest is not None:
        _validate_finance_manifest(pending_manifest, expected_manifest)
        if presence == expected_counts:
            if pending_review_path.exists() and resolved_review_path.exists():
                raise LegacyClinicImportError(
                    "Há duas versões da planilha de conferência durante a recuperação"
                )
            try:
                if pending_review_path.exists():
                    pending_review_path.replace(resolved_review_path)
                elif not resolved_review_path.exists():
                    _write_bytes_atomic(resolved_review_path, review_payload)
                pending_manifest_path.replace(resolved_manifest_path)
            except OSError as exc:
                raise LegacyClinicImportError(
                    "Não foi possível concluir a recuperação dos artefatos financeiros"
                ) from exc
            return LegacyFinanceImportResult(
                professional_id=professional.id,
                source_sha256=source_sha256,
                manifest_path=str(resolved_manifest_path),
                review_csv_path=str(resolved_review_path),
                created_counts=empty_counts,
                skipped_counts=expected_counts,
                verified_counts=expected_counts,
                paid_total_cents=paid_total_cents,
                review_records=len(plan.review),
                review_total_cents=review_total_cents,
            )
        if not any(financial_presence.values()):
            pending_manifest_path.unlink(missing_ok=True)
            pending_review_path.unlink(missing_ok=True)
        else:
            raise LegacyClinicImportError(
                "A recuperação encontrou somente parte dos destinos financeiros esperados"
            )
    if any(financial_presence.values()):
        raise LegacyClinicImportError(
            "Registros financeiros determinísticos já existem sem o manifesto local"
        )
    if resolved_review_path.exists():
        raise LegacyClinicImportError(
            "A planilha de conferência já existe sem o manifesto financeiro correspondente"
        )

    _write_bytes_atomic(pending_review_path, review_payload)
    _write_manifest(pending_manifest_path, expected_manifest)

    created_counts = {key: 0 for key in expected_counts}
    committed = False
    try:
        if method is None:
            db.add(
                PaymentMethod(
                    id=method_id,
                    professional_id=professional.id,
                    name=_PAYMENT_METHOD_NAME,
                    active=True,
                )
            )
            created_counts["payment_methods"] = 1
            await db.flush()

        audit_created_at = datetime.now(UTC)
        for source in plan.paid:
            source_id = str(source["id"])
            ids = _finance_target_ids(namespace, source_id)
            appointment_id = _target_uuid(namespace, "appointment", source_id)
            target_appointment = target_appointments[appointment_id]
            patient = patients_by_source[str(source["paciente_id"])]
            procedure = procedures_by_source.get(str(source.get("procedimento_id")))
            payer_name, payer_document = _payer_details(patient)
            starts_at = _parse_datetime(
                source.get("dt_inicial"), "agendamento.dt_inicial"
            )
            amount_cents = _money_to_cents(
                source.get("valor_total"), "agendamento.valor_total"
            )
            if amount_cents is None or amount_cents <= 0:
                raise LegacyClinicImportError(
                    f"Valor inválido no pagamento de origem {source_id}"
                )
            service_name = (
                " ".join((procedure.get("nome") or "").split())
                if procedure
                else target_appointment.service_name_snapshot or "Atendimento importado"
            )
            patient_name = " ".join((patient.get("nome") or "").split())
            notes = (
                "Importado do sistema anterior. Data de pagamento estimada pela data "
                "do atendimento; forma de pagamento não informada na origem."
            )
            receivable = Receivable(
                id=ids["receivables"],
                professional_id=professional.id,
                patient_id=target_appointment.patient_id,
                category_id=None,
                patient_name_snapshot=patient_name,
                payer_name=payer_name,
                payer_document=payer_document,
                description=service_name[:255],
                issue_date=starts_at.date(),
                competence_date=starts_at.date(),
                due_date=starts_at.date(),
                total_cents=amount_cents,
                status="paid",
                origin="appointment",
                notes=notes,
            )
            payment = FinancialPayment(
                id=ids["payments"],
                professional_id=professional.id,
                patient_id=target_appointment.patient_id,
                method_id=method_id,
                patient_name_snapshot=patient_name,
                payer_name=payer_name,
                payer_document=payer_document,
                payment_date=starts_at.date(),
                amount_cents=amount_cents,
                status="confirmed",
                receipt_number=f"MIG-{professional.id.hex[:8]}-{source_id}"[:40],
                notes=(
                    "Importado do sistema anterior. Forma de pagamento não informada; "
                    "data de pagamento estimada pela data do atendimento."
                ),
            )
            db.add_all([receivable, payment])
            await db.flush()
            db.add_all(
                [
                    ReceivableItem(
                        id=ids["receivable_items"],
                        receivable_id=ids["receivables"],
                        service_id=target_appointment.service_id,
                        appointment_id=appointment_id,
                        item_type="service",
                        description=service_name[:255],
                        quantity=1,
                        unit_cents=amount_cents,
                        total_cents=amount_cents,
                    ),
                    PaymentAllocation(
                        id=ids["allocations"],
                        payment_id=ids["payments"],
                        receivable_id=ids["receivables"],
                        amount_cents=amount_cents,
                    ),
                    FinancialAuditEvent(
                        id=ids["receivable_audit"],
                        professional_id=professional.id,
                        entity_type="receivable",
                        entity_id=ids["receivables"],
                        action="imported_paid",
                        payload={"amountCents": amount_cents, "source": _SOURCE_SYSTEM},
                        created_at=audit_created_at,
                    ),
                    FinancialAuditEvent(
                        id=ids["payment_audit"],
                        professional_id=professional.id,
                        entity_type="payment",
                        entity_id=ids["payments"],
                        action="imported_confirmed",
                        payload={"amountCents": amount_cents, "source": _SOURCE_SYSTEM},
                        created_at=audit_created_at,
                    ),
                ]
            )
            created_counts["receivables"] += 1
            created_counts["receivable_items"] += 1
            created_counts["payments"] += 1
            created_counts["allocations"] += 1
            created_counts["audit_events"] += 2

        await db.commit()
        committed = True
    except Exception:
        await db.rollback()
        if not committed:
            pending_manifest_path.unlink(missing_ok=True)
            pending_review_path.unlink(missing_ok=True)
        raise

    verified_counts = await _presence_counts(
        db,
        method_id=method_id,
        target_ids=target_ids,
    )
    if verified_counts != expected_counts:
        raise LegacyClinicImportError(
            "A transação financeira foi confirmada, mas a contagem pós-commit divergiu"
        )
    received_total = int(
        await db.scalar(
            select(func.coalesce(func.sum(FinancialPayment.amount_cents), 0)).where(
                FinancialPayment.id.in_(target_ids["payments"]),
                FinancialPayment.status == "confirmed",
            )
        )
        or 0
    )
    receivable_total = int(
        await db.scalar(
            select(func.coalesce(func.sum(Receivable.total_cents), 0)).where(
                Receivable.id.in_(target_ids["receivables"]),
                Receivable.status == "paid",
            )
        )
        or 0
    )
    allocation_total = int(
        await db.scalar(
            select(func.coalesce(func.sum(PaymentAllocation.amount_cents), 0)).where(
                PaymentAllocation.id.in_(target_ids["allocations"])
            )
        )
        or 0
    )
    if {received_total, receivable_total, allocation_total} != {paid_total_cents}:
        raise LegacyClinicImportError(
            "A verificação pós-commit encontrou divergência nos totais financeiros"
        )
    try:
        pending_review_path.replace(resolved_review_path)
        pending_manifest_path.replace(resolved_manifest_path)
    except OSError as exc:
        raise LegacyClinicImportError(
            "Os dados financeiros foram confirmados, mas os artefatos locais ficaram pendentes"
        ) from exc

    return LegacyFinanceImportResult(
        professional_id=professional.id,
        source_sha256=source_sha256,
        manifest_path=str(resolved_manifest_path),
        review_csv_path=str(resolved_review_path),
        created_counts=created_counts,
        skipped_counts=empty_counts,
        verified_counts=verified_counts,
        paid_total_cents=paid_total_cents,
        review_records=len(plan.review),
        review_total_cents=review_total_cents,
    )
