"""Safe import planning for the supported legacy clinic SQL export."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AVATAR_COLORS
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.finance import ServiceOffering
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session

SUPPORTED_TABLES = (
    "agendamento",
    "agendamento_evolucao",
    "agendamento_evolucao_auditoria",
    "pacientes",
    "procedimento",
    "system_users",
)

INFERRED_APPOINTMENT_STATE_MAP = {
    "1": "pendente",
    "2": "confirmado",
    "3": "cancelado",
    "4": "concluido",
    "5": "cancelado",
    "6": "cancelado",
    "15": "pendente",
}

_INSERT_PATTERN = re.compile(
    r"^INSERT INTO `([^`]+)` \((.*)\) VALUES \((.*)\);$"
)
_CLINIC_TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
_SOURCE_SYSTEM = "legacy_clinic_sql"
_MANIFEST_SCHEMA_VERSION = 1


class LegacyClinicImportError(ValueError):
    """Raised when a legacy export cannot be imported safely."""


@dataclass(frozen=True)
class LegacyClinicImportPreview:
    professional_id: UUID
    source_sha256: str
    manifest_path: str
    source_counts: dict[str, int]
    projected_counts: dict[str, int]
    appointment_status_counts: dict[str, int]
    warnings: list[str]


@dataclass(frozen=True)
class LegacyClinicImportResult:
    professional_id: UUID
    source_sha256: str
    manifest_path: str
    created_counts: dict[str, int]
    skipped_counts: dict[str, int]
    verified_counts: dict[str, int]


@dataclass(frozen=True)
class _ManifestSpec:
    source_table: str
    source_id: str
    target_table: str
    target_id: UUID
    payload_sha256: str
    source_metadata: dict | None = None


def default_legacy_import_manifest_path(source_path: Path) -> Path:
    """Return the local, non-clinical audit manifest path for an export."""

    path = Path(source_path)
    return path.with_name(f"{path.name}.korus-import.json")


def _pending_manifest_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.name}.pending")


def _read_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyClinicImportError(
            "O manifesto local de importação não pôde ser lido com segurança"
        ) from exc
    if not isinstance(payload, dict):
        raise LegacyClinicImportError("O manifesto local de importação é inválido")
    return payload


def _write_manifest(path: Path, payload: dict) -> None:
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise LegacyClinicImportError(
            "Não foi possível gravar o manifesto local de importação"
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def _split_sql_values(raw: str) -> list[str | None]:
    values: list[str | None] = []
    buffer: list[str] = []
    quoted = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if quoted:
            if char == "'":
                if index + 1 < len(raw) and raw[index + 1] == "'":
                    buffer.append("'")
                    index += 2
                    continue
                quoted = False
                index += 1
                continue
            if char == "\\" and index + 1 < len(raw):
                escaped = raw[index + 1]
                buffer.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(escaped, escaped))
                index += 2
                continue
            buffer.append(char)
            index += 1
            continue

        if char == "'":
            quoted = True
            index += 1
            continue
        if char == ",":
            token = "".join(buffer).strip()
            values.append(None if token.upper() == "NULL" else token)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    if quoted:
        raise LegacyClinicImportError("Literal SQL não terminado no arquivo de origem")
    token = "".join(buffer).strip()
    values.append(None if token.upper() == "NULL" else token)
    return values


def _parse_legacy_sql(source_path: Path) -> tuple[str, dict[str, list[dict[str, str | None]]]]:
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise LegacyClinicImportError(f"Não foi possível ler o arquivo: {exc}") from exc

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LegacyClinicImportError("O arquivo precisa estar codificado em UTF-8") from exc

    rows: dict[str, list[dict[str, str | None]]] = {table: [] for table in SUPPORTED_TABLES}
    seen_ids: dict[str, set[str]] = {table: set() for table in SUPPORTED_TABLES}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        match = _INSERT_PATTERN.match(line)
        if not match:
            raise LegacyClinicImportError(
                f"Comando SQL não suportado na linha {line_number}; o arquivo não será executado"
            )
        table, raw_columns, raw_values = match.groups()
        if table not in rows:
            raise LegacyClinicImportError(
                f"Tabela não suportada na linha {line_number}: {table}"
            )
        columns = [column.strip().strip("`") for column in raw_columns.split(",")]
        values = _split_sql_values(raw_values)
        if len(columns) != len(values):
            raise LegacyClinicImportError(
                f"Quantidade de colunas e valores diverge na linha {line_number}"
            )
        row = dict(zip(columns, values, strict=True))
        source_id = row.get("id")
        if not source_id:
            raise LegacyClinicImportError(
                f"Registro sem id de origem na linha {line_number}"
            )
        if source_id in seen_ids[table]:
            raise LegacyClinicImportError(
                f"ID de origem duplicado em {table}: {source_id}"
            )
        seen_ids[table].add(source_id)
        rows[table].append(row)

    return hashlib.sha256(payload).hexdigest(), rows


def _parse_datetime(value: str | None, field: str) -> datetime:
    if not value:
        raise LegacyClinicImportError(f"Data obrigatória ausente: {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LegacyClinicImportError(f"Data inválida em {field}: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CLINIC_TIMEZONE)
    return parsed


def _parse_optional_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    return _parse_datetime(value, field).date()


def _money_to_cents(value: str | None, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise LegacyClinicImportError(f"Valor monetário inválido em {field}: {value}") from exc
    if not amount.is_finite():
        raise LegacyClinicImportError(f"Valor monetário inválido em {field}: {value}")
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _duration_minutes(start: datetime, end_value: str | None, fallback: int = 50) -> int:
    if end_value:
        end = _parse_datetime(end_value, "agendamento.dt_final")
        minutes = int((end - start).total_seconds() // 60)
        if 0 < minutes <= 24 * 60:
            return minutes
    return fallback


def _procedure_duration(value: str | None) -> int:
    if not value:
        return 50
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
    except (TypeError, ValueError) as exc:
        raise LegacyClinicImportError(f"Duração de procedimento inválida: {value}") from exc
    duration = hours * 60 + minutes + (1 if seconds >= 30 else 0)
    if duration <= 0 or duration > 24 * 60:
        raise LegacyClinicImportError(f"Duração de procedimento inválida: {value}")
    return duration


def _procedure_price_cents(procedure: dict[str, str | None]) -> int | None:
    price_cents = _money_to_cents(
        procedure.get("valor_total") or procedure.get("valor"),
        "procedimento.valor_total",
    )
    return price_cents if price_cents and price_cents > 0 else None


def _importable_procedures(
    procedures: list[dict[str, str | None]],
    appointments: list[dict[str, str | None]],
) -> tuple[list[dict[str, str | None]], int, int]:
    imported: list[dict[str, str | None]] = []
    skipped_ids: set[str] = set()
    for procedure in procedures:
        source_id = str(procedure["id"])
        if _procedure_price_cents(procedure) is None:
            skipped_ids.add(source_id)
            continue
        imported.append(procedure)
    linked_appointment_count = sum(
        str(appointment.get("procedimento_id")) in skipped_ids
        for appointment in appointments
    )
    return imported, len(skipped_ids), linked_appointment_count


class _LegacyHtmlToText(HTMLParser):
    _BLOCK_TAGS = {"br", "div", "p", "li", "ol", "ul", "h1", "h2", "h3", "h4"}
    _IGNORED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in self._IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _html_to_plain_text(value: str | None) -> str:
    parser = _LegacyHtmlToText()
    parser.feed(value or "")
    parser.close()
    lines = []
    for raw_line in "".join(parser.parts).replace("\xa0", " ").splitlines():
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().upper() in {"1", "S", "SIM", "T", "TRUE", "Y"}


def _target_uuid(namespace: UUID, entity: str, source_id: str) -> UUID:
    return uuid5(namespace, f"{entity}:{source_id}")


def _payload_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _manifest_payload(
    *,
    professional_id: UUID,
    source_sha256: str,
    specs: list[_ManifestSpec],
    verified_counts: dict[str, int],
) -> dict:
    records = [
        {
            "payloadSha256": spec.payload_sha256,
            "sourceId": spec.source_id,
            "sourceMetadata": spec.source_metadata,
            "sourceTable": spec.source_table,
            "targetId": str(spec.target_id),
            "targetTable": spec.target_table,
        }
        for spec in sorted(specs, key=lambda item: (item.source_table, item.source_id))
    ]
    return {
        "professionalId": str(professional_id),
        "records": records,
        "schemaVersion": _MANIFEST_SCHEMA_VERSION,
        "sourceFileSha256": source_sha256,
        "sourceSystem": _SOURCE_SYSTEM,
        "verifiedCounts": verified_counts,
    }


def _validate_manifest(
    manifest: dict,
    *,
    professional_id: UUID,
    source_sha256: str,
    specs: list[_ManifestSpec],
) -> None:
    if (
        manifest.get("schemaVersion") != _MANIFEST_SCHEMA_VERSION
        or manifest.get("sourceSystem") != _SOURCE_SYSTEM
        or manifest.get("professionalId") != str(professional_id)
    ):
        raise LegacyClinicImportError(
            "O manifesto local não corresponde à profissional ou ao importador atual"
        )
    raw_records = manifest.get("records")
    if not isinstance(raw_records, list):
        raise LegacyClinicImportError("O manifesto local de importação é inválido")
    records: dict[tuple[str, str], dict] = {}
    for record in raw_records:
        if not isinstance(record, dict):
            raise LegacyClinicImportError("O manifesto local de importação é inválido")
        key = (str(record.get("sourceTable")), str(record.get("sourceId")))
        if key in records:
            raise LegacyClinicImportError("O manifesto local contém registros duplicados")
        records[key] = record

    expected_keys = {(spec.source_table, spec.source_id) for spec in specs}
    if set(records) != expected_keys:
        raise LegacyClinicImportError(
            "O conjunto de registros do arquivo mudou desde a importação anterior"
        )
    for spec in specs:
        record = records[(spec.source_table, spec.source_id)]
        if (
            record.get("payloadSha256") != spec.payload_sha256
            or record.get("targetTable") != spec.target_table
            or record.get("targetId") != str(spec.target_id)
        ):
            raise LegacyClinicImportError(
                f"O registro {spec.source_table}/{spec.source_id} mudou desde a importação anterior"
            )
    if manifest.get("sourceFileSha256") != source_sha256:
        raise LegacyClinicImportError(
            "O arquivo SQL mudou desde a importação registrada no manifesto local"
        )


def _target_presence_by_manifest_spec(
    specs: list[_ManifestSpec],
    target_ids_by_table: dict[str, set[UUID]],
) -> list[bool]:
    return [
        spec.target_id in target_ids_by_table[spec.target_table]
        for spec in specs
    ]


async def _existing_ids(db: AsyncSession, model, ids: list[UUID]) -> set[UUID]:
    existing: set[UUID] = set()
    for offset in range(0, len(ids), 500):
        chunk = ids[offset : offset + 500]
        if not chunk:
            continue
        result = await db.execute(select(model.id).where(model.id.in_(chunk)))
        existing.update(result.scalars().all())
    return existing


def _import_namespace(professional_id: UUID, rows: dict[str, list[dict[str, str | None]]]) -> UUID:
    source_professional_ids = {
        str(row.get("profissional_id"))
        for row in rows["agendamento"]
        if row.get("profissional_id") is not None
    }
    if len(source_professional_ids) != 1:
        raise LegacyClinicImportError(
            "O arquivo deve conter agendamentos de exatamente uma profissional de origem"
        )
    source_professional_id = next(iter(source_professional_ids))
    return uuid5(
        NAMESPACE_URL,
        f"korus:legacy-clinic:{professional_id}:{source_professional_id}",
    )


def _caregiver_specs(patient: dict[str, str | None]) -> list[dict[str, object]]:
    candidates = [
        (
            patient.get("responsavel_nome"),
            "Responsável",
            patient.get("responsavel_telefone") or patient.get("telefone"),
            patient.get("responsavel_email"),
        ),
        (
            patient.get("responsavel_nome_2"),
            "Responsável",
            patient.get("responsavel_telefone_2"),
            patient.get("responsavel_email_2"),
        ),
        (
            patient.get("mae_nome"),
            "Mãe",
            patient.get("mae_telefone"),
            patient.get("mae_email"),
        ),
        (
            patient.get("pai_nome"),
            "Pai",
            patient.get("pai_telefone"),
            patient.get("pai_email"),
        ),
    ]
    specs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for name, relation, phone, email in candidates:
        normalized_name = " ".join((name or "").split())
        if not normalized_name:
            continue
        normalized_phone = re.sub(r"\D", "", phone or "")
        key = (normalized_name.casefold(), normalized_phone)
        if key in seen:
            continue
        seen.add(key)
        specs.append(
            {
                "name": normalized_name,
                "relation": relation,
                "phone": normalized_phone,
                "email": (email or "").strip(),
            }
        )
    return specs


def _validate_and_count_statuses(
    appointments: list[dict[str, str | None]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for appointment in appointments:
        counts[_target_appointment_status(appointment)] += 1
    return counts


def _target_appointment_status(appointment: dict[str, str | None]) -> str:
    if not _is_true(appointment.get("ativo")):
        return "cancelado"
    source_state = appointment.get("estado_agenda_id")
    try:
        return INFERRED_APPOINTMENT_STATE_MAP[str(source_state)]
    except KeyError as exc:
        raise LegacyClinicImportError(
            f"Estado de agenda sem mapeamento seguro: {source_state}"
        ) from exc


async def _resolve_professional(db: AsyncSession, email: str) -> Professional:
    normalized_email = email.strip().lower()
    result = await db.execute(
        select(Professional)
        .where(func.lower(Professional.email) == normalized_email)
        .limit(2)
    )
    matches = list(result.scalars().all())
    if not matches:
        raise LegacyClinicImportError("Profissional não encontrada pelo e-mail informado")
    if len(matches) > 1:
        raise LegacyClinicImportError("Mais de uma profissional encontrada para o e-mail informado")
    return matches[0]


async def preview_legacy_clinic_import(
    db: AsyncSession,
    *,
    source_path: Path,
    professional_email: str,
) -> LegacyClinicImportPreview:
    """Validate a legacy export and project its import without writing to the database."""

    professional = await _resolve_professional(db, professional_email)
    source_sha256, rows = _parse_legacy_sql(Path(source_path))

    appointment_status_counts = _validate_and_count_statuses(rows["agendamento"])
    (
        imported_procedures,
        skipped_unpriced_procedure_count,
        unpriced_procedure_appointment_count,
    ) = _importable_procedures(rows["procedimento"], rows["agendamento"])

    active_audit_ids = {
        audit["id"]
        for audit in rows["agendamento_evolucao_auditoria"]
        if audit.get("ativo") == "T"
    }
    active_evolution_count = sum(
        evolution.get("evolucao_auditoria_id") in active_audit_ids
        for evolution in rows["agendamento_evolucao"]
    )
    caregiver_count = sum(len(_caregiver_specs(patient)) for patient in rows["pacientes"])
    session_count = sum(
        appointment.get("estado_agenda_id") == "4"
        and _is_true(appointment.get("ativo"))
        for appointment in rows["agendamento"]
    )

    warnings = [
        "O mapeamento dos estados da agenda foi inferido e exige aceite explícito para aplicar."
    ]
    if skipped_unpriced_procedure_count:
        singular = skipped_unpriced_procedure_count == 1
        noun = "procedimento" if singular else "procedimentos"
        verb = "será ignorado" if singular else "serão ignorados"
        create_verb = "será criado" if singular else "serão criados"
        if unpriced_procedure_appointment_count:
            warnings.append(
                f"{skipped_unpriced_procedure_count} {noun} sem preço não "
                f"{create_verb} "
                "como serviço financeiro."
            )
        else:
            warnings.append(
                f"{skipped_unpriced_procedure_count} {noun} sem preço e sem "
                f"agendamentos {verb}."
            )
    if unpriced_procedure_appointment_count:
        warnings.append(
            f"{unpriced_procedure_appointment_count} agendamentos com procedimento sem "
            "preço manterão o snapshot da origem sem vínculo a serviço financeiro."
        )

    return LegacyClinicImportPreview(
        professional_id=professional.id,
        source_sha256=source_sha256,
        manifest_path=str(default_legacy_import_manifest_path(Path(source_path))),
        source_counts={table: len(rows[table]) for table in SUPPORTED_TABLES},
        projected_counts={
            "appointments": len(rows["agendamento"]),
            "caregivers": caregiver_count,
            "evolutions": active_evolution_count,
            "patients": len(rows["pacientes"]),
            "services": len(imported_procedures),
            "sessions": session_count,
        },
        appointment_status_counts=dict(sorted(appointment_status_counts.items())),
        warnings=warnings,
    )


async def apply_legacy_clinic_import(
    db: AsyncSession,
    *,
    source_path: Path,
    professional_email: str,
    accept_inferred_state_map: bool,
    expected_source_sha256: str | None = None,
    manifest_path: Path | None = None,
) -> LegacyClinicImportResult:
    """Apply a validated import after explicit acceptance of inferred states."""

    if not accept_inferred_state_map:
        raise LegacyClinicImportError(
            "Confirme explicitamente o mapeamento inferido dos estados da agenda"
        )
    professional = await _resolve_professional(db, professional_email)
    notification_settings = await db.scalar(
        select(NotificationSettings).where(
            NotificationSettings.professional_id == professional.id
        )
    )
    if (
        notification_settings
        and notification_settings.whatsapp_enabled
        and bool(
            (notification_settings.whatsapp_events or {}).get(
                "appointment_reminder_24h"
            )
        )
    ):
        raise LegacyClinicImportError(
            "Desative temporariamente o lembrete automático de 24 horas antes de aplicar"
        )
    source_sha256, rows = _parse_legacy_sql(Path(source_path))
    resolved_manifest_path = Path(
        manifest_path or default_legacy_import_manifest_path(Path(source_path))
    )
    if resolved_manifest_path.resolve() == Path(source_path).resolve():
        raise LegacyClinicImportError(
            "O manifesto local deve ser diferente do arquivo SQL de origem"
        )
    if not expected_source_sha256:
        raise LegacyClinicImportError(
            "Informe o SHA-256 exibido no dry-run revisado"
        )
    if (
        expected_source_sha256.strip().lower() != source_sha256
    ):
        raise LegacyClinicImportError(
            "O SHA-256 do arquivo não corresponde ao dry-run revisado"
        )
    _validate_and_count_statuses(rows["agendamento"])
    namespace = _import_namespace(professional.id, rows)

    patients_by_source = {row["id"]: row for row in rows["pacientes"]}
    all_procedures_by_source = {row["id"]: row for row in rows["procedimento"]}
    appointments_by_source = {row["id"]: row for row in rows["agendamento"]}
    if any(
        appointment.get("paciente_id") not in patients_by_source
        for appointment in rows["agendamento"]
    ):
        raise LegacyClinicImportError("Há agendamento apontando para paciente ausente no arquivo")
    if any(
        appointment.get("procedimento_id")
        and appointment.get("procedimento_id") not in all_procedures_by_source
        for appointment in rows["agendamento"]
    ):
        raise LegacyClinicImportError(
            "Há agendamento apontando para procedimento ausente no arquivo"
        )
    imported_procedures, _, _ = _importable_procedures(
        rows["procedimento"], rows["agendamento"]
    )
    procedures_by_source = all_procedures_by_source

    earliest_appointment: dict[str, date] = {}
    for appointment in rows["agendamento"]:
        patient_source_id = str(appointment["paciente_id"])
        appointment_date = _parse_datetime(
            appointment.get("dt_inicial"), "agendamento.dt_inicial"
        ).date()
        current = earliest_appointment.get(patient_source_id)
        if current is None or appointment_date < current:
            earliest_appointment[patient_source_id] = appointment_date

    patient_ids = {
        source_id: _target_uuid(namespace, "patient", source_id)
        for source_id in patients_by_source
    }
    source_patient_keys = Counter(
        (
            " ".join((patient.get("nome") or "").split()).casefold(),
            _parse_datetime(
                patient.get("dt_nascimento"), "pacientes.dt_nascimento"
            ).date(),
        )
        for patient in rows["pacientes"]
    )
    if any(count > 1 for count in source_patient_keys.values()):
        raise LegacyClinicImportError(
            "O arquivo contém pacientes duplicados por nome e data de nascimento"
        )
    target_patients_result = await db.execute(
        select(Patient).where(Patient.professional_id == professional.id)
    )
    planned_patient_ids = set(patient_ids.values())
    for target_patient in target_patients_result.scalars().all():
        if target_patient.id in planned_patient_ids:
            continue
        target_key = (
            " ".join(target_patient.name.split()).casefold(),
            target_patient.birth_date,
        )
        if target_key in source_patient_keys:
            raise LegacyClinicImportError(
                "Já existe paciente com o mesmo nome e data de nascimento na conta de destino"
            )
    service_ids = {
        str(procedure["id"]): _target_uuid(
            namespace, "service", str(procedure["id"])
        )
        for procedure in imported_procedures
    }
    appointment_ids = {
        source_id: _target_uuid(namespace, "appointment", source_id)
        for source_id in appointments_by_source
    }
    session_ids = {
        source_id: _target_uuid(namespace, "session", source_id)
        for source_id, appointment in appointments_by_source.items()
        if appointment.get("estado_agenda_id") == "4"
        and _is_true(appointment.get("ativo"))
    }
    caregiver_entries = [
        (
            patient,
            index,
            spec,
            _target_uuid(namespace, "caregiver", f"{patient['id']}:{index}"),
        )
        for patient in rows["pacientes"]
        for index, spec in enumerate(_caregiver_specs(patient))
    ]
    active_audits_by_id = {
        audit["id"]: audit
        for audit in rows["agendamento_evolucao_auditoria"]
        if _is_true(audit.get("ativo"))
    }
    evolution_entries = [
        (
            source,
            active_audits_by_id[source["evolucao_auditoria_id"]],
            _target_uuid(namespace, "evolution", str(source["id"])),
        )
        for source in rows["agendamento_evolucao"]
        if source.get("evolucao_auditoria_id") in active_audits_by_id
    ]

    existing_patients = await _existing_ids(db, Patient, list(patient_ids.values()))
    existing_caregivers = await _existing_ids(
        db, Caregiver, [entry[3] for entry in caregiver_entries]
    )
    existing_services = await _existing_ids(db, ServiceOffering, list(service_ids.values()))
    existing_appointments = await _existing_ids(
        db, Appointment, list(appointment_ids.values())
    )
    existing_sessions = await _existing_ids(db, Session, list(session_ids.values()))
    existing_evolutions = await _existing_ids(
        db, Evolution, [entry[2] for entry in evolution_entries]
    )

    manifest_specs = [
        _ManifestSpec(
            source_table="pacientes",
            source_id=str(patient["id"]),
            target_table="patients",
            target_id=patient_ids[str(patient["id"])],
            payload_sha256=_payload_sha256(patient),
        )
        for patient in rows["pacientes"]
    ]
    manifest_specs.extend(
        _ManifestSpec(
            source_table="pacientes",
            source_id=f"{patient['id']}:caregiver:{index}",
            target_table="caregivers",
            target_id=caregiver_id,
            payload_sha256=_payload_sha256(
                {"patient": patient, "caregiverIndex": index, "spec": spec}
            ),
        )
        for patient, index, spec, caregiver_id in caregiver_entries
    )
    manifest_specs.extend(
        _ManifestSpec(
            source_table="procedimento",
            source_id=str(procedure["id"]),
            target_table="financial_services",
            target_id=service_ids[str(procedure["id"])],
            payload_sha256=_payload_sha256(procedure),
        )
        for procedure in imported_procedures
    )
    manifest_specs.extend(
        _ManifestSpec(
            source_table="agendamento",
            source_id=str(appointment["id"]),
            target_table="appointments",
            target_id=appointment_ids[str(appointment["id"])],
            payload_sha256=_payload_sha256(appointment),
            source_metadata={"stateId": appointment.get("estado_agenda_id")},
        )
        for appointment in rows["agendamento"]
    )
    manifest_specs.extend(
        _ManifestSpec(
            source_table="agendamento",
            source_id=f"{source_id}:session",
            target_table="sessions",
            target_id=target_id,
            payload_sha256=_payload_sha256(appointments_by_source[source_id]),
            source_metadata={
                "stateId": appointments_by_source[source_id].get("estado_agenda_id")
            },
        )
        for source_id, target_id in session_ids.items()
    )
    manifest_specs.extend(
        _ManifestSpec(
            source_table="agendamento_evolucao",
            source_id=str(source["id"]),
            target_table="evolutions",
            target_id=evolution_id,
            payload_sha256=_payload_sha256({"evolution": source, "audit": audit}),
        )
        for source, audit, evolution_id in evolution_entries
    )

    target_ids_by_table = {
        "patients": existing_patients,
        "caregivers": existing_caregivers,
        "financial_services": existing_services,
        "appointments": existing_appointments,
        "sessions": existing_sessions,
        "evolutions": existing_evolutions,
    }
    expected_counts = {
        "appointments": len(appointment_ids),
        "caregivers": len(caregiver_entries),
        "evolutions": len(evolution_entries),
        "patients": len(patient_ids),
        "services": len(service_ids),
        "sessions": len(session_ids),
    }
    empty_counts = {key: 0 for key in expected_counts}
    manifest_payload = _manifest_payload(
        professional_id=professional.id,
        source_sha256=source_sha256,
        specs=manifest_specs,
        verified_counts=expected_counts,
    )
    pending_manifest_path = _pending_manifest_path(resolved_manifest_path)
    manifest = _read_manifest(resolved_manifest_path)
    pending_manifest = _read_manifest(pending_manifest_path)
    if manifest is not None and pending_manifest is not None:
        raise LegacyClinicImportError(
            "Há um manifesto local pendente junto de uma importação concluída; revise os arquivos"
        )

    target_presence = _target_presence_by_manifest_spec(
        manifest_specs, target_ids_by_table
    )
    if manifest is None and pending_manifest is not None:
        _validate_manifest(
            pending_manifest,
            professional_id=professional.id,
            source_sha256=source_sha256,
            specs=manifest_specs,
        )
        if target_presence and all(target_presence):
            try:
                pending_manifest_path.replace(resolved_manifest_path)
            except OSError as exc:
                raise LegacyClinicImportError(
                    "Não foi possível concluir a recuperação do manifesto local"
                ) from exc
            manifest = pending_manifest
        elif any(target_presence):
            raise LegacyClinicImportError(
                "A recuperação encontrou somente parte dos destinos do manifesto pendente"
            )
        else:
            pending_manifest_path.unlink(missing_ok=True)

    if manifest is not None:
        _validate_manifest(
            manifest,
            professional_id=professional.id,
            source_sha256=source_sha256,
            specs=manifest_specs,
        )
        for spec, target_exists in zip(
            manifest_specs, target_presence, strict=True
        ):
            if not target_exists:
                raise LegacyClinicImportError(
                    f"O destino rastreado de {spec.source_table}/{spec.source_id} não existe mais"
                )
        return LegacyClinicImportResult(
            professional_id=professional.id,
            source_sha256=source_sha256,
            manifest_path=str(resolved_manifest_path),
            created_counts=empty_counts,
            skipped_counts=expected_counts,
            verified_counts=expected_counts,
        )

    if any(target_presence):
        raise LegacyClinicImportError(
            "Registros determinísticos já existem no destino sem o manifesto local"
        )

    _write_manifest(pending_manifest_path, manifest_payload)

    created_counts = {
        "appointments": 0,
        "caregivers": 0,
        "evolutions": 0,
        "patients": 0,
        "services": 0,
        "sessions": 0,
    }
    skipped_counts = {key: 0 for key in created_counts}

    try:
        for index, patient in enumerate(rows["pacientes"]):
            source_id = str(patient["id"])
            if patient_ids[source_id] in existing_patients:
                skipped_counts["patients"] += 1
                continue
            created_at = _parse_datetime(
                patient.get("data_criacao"), "pacientes.data_criacao"
            )
            start_date = earliest_appointment.get(source_id, created_at.date())
            db.add(
                Patient(
                    id=patient_ids[source_id],
                    professional_id=professional.id,
                    name=" ".join((patient.get("nome") or "").split()),
                    birth_date=_parse_datetime(
                        patient.get("dt_nascimento"), "pacientes.dt_nascimento"
                    ).date(),
                    diagnosis_keys=["nao_informado"],
                    status="ativo" if _is_true(patient.get("ativo")) else "pausado",
                    start_date=start_date,
                    avatar_color=AVATAR_COLORS[index % len(AVATAR_COLORS)],
                    is_demo=False,
                    created_at=created_at,
                )
            )
            created_counts["patients"] += 1
        await db.flush()

        for patient, index, spec, caregiver_id in caregiver_entries:
            if caregiver_id in existing_caregivers:
                skipped_counts["caregivers"] += 1
                continue
            source_patient_id = str(patient["id"])
            db.add(
                Caregiver(
                    id=caregiver_id,
                    patient_id=patient_ids[source_patient_id],
                    name=str(spec["name"]),
                    relation=str(spec["relation"]),
                    phone=str(spec["phone"]),
                    email=str(spec["email"]),
                    notes="",
                    is_primary=index == 0,
                    whatsapp_opt_in=(
                        index == 0
                        and _is_true(
                            patient.get("aceita_receber_mensagen_whatsapp")
                        )
                    ),
                )
            )
            created_counts["caregivers"] += 1

        service_durations = {
            str(procedure["id"]): _procedure_duration(procedure.get("duracao"))
            for procedure in rows["procedimento"]
        }
        for procedure in imported_procedures:
            source_id = str(procedure["id"])
            duration = service_durations[source_id]
            if service_ids[source_id] in existing_services:
                skipped_counts["services"] += 1
                continue
            price_cents = _procedure_price_cents(procedure)
            if price_cents is None:
                raise LegacyClinicImportError(
                    f"Procedimento sem valor: {source_id}"
                )
            db.add(
                ServiceOffering(
                    id=service_ids[source_id],
                    professional_id=professional.id,
                    category_id=None,
                    name=" ".join((procedure.get("nome") or "").split())[:120],
                    description="Importado da plataforma anterior.",
                    duration=duration,
                    price_cents=price_cents,
                    active=_is_true(procedure.get("ativo")),
                )
            )
            created_counts["services"] += 1
        await db.flush()

        def appointment_entity(source: dict[str, str | None]) -> Appointment:
            source_id = str(source["id"])
            procedure_source_id = source.get("procedimento_id")
            procedure = (
                procedures_by_source.get(procedure_source_id)
                if procedure_source_id
                else None
            )
            starts_at = _parse_datetime(source.get("dt_inicial"), "agendamento.dt_inicial")
            fallback_duration = service_durations.get(str(procedure_source_id), 50)
            recurrence = source.get("tipo_repeticao")
            return Appointment(
                id=appointment_ids[source_id],
                professional_id=professional.id,
                patient_id=patient_ids[str(source["paciente_id"])],
                service_id=service_ids.get(str(procedure_source_id)),
                service_name_snapshot=(
                    " ".join((procedure.get("nome") or "").split())[:120]
                    if procedure
                    else None
                ),
                service_price_cents=_money_to_cents(
                    source.get("valor_total"), "agendamento.valor_total"
                ),
                date=starts_at.date(),
                time=starts_at.timetz().replace(tzinfo=None),
                type=(
                    " ".join((procedure.get("nome") or "").split())[:100]
                    if procedure
                    else "Atendimento importado"
                ),
                duration=_duration_minutes(
                    starts_at, source.get("dt_final"), fallback_duration
                ),
                status=_target_appointment_status(source),
                appointment_type=(
                    "recorrente"
                    if source.get("agendamento_pai_id") or recurrence
                    else "avulso"
                ),
                series_id=(
                    appointment_ids.get(str(source.get("agendamento_pai_id")))
                    if source.get("agendamento_pai_id")
                    else None
                ),
                frequency={"S": "semanal", "Q15": "quinzenal"}.get(str(recurrence)),
                end_date=_parse_optional_date(
                    source.get("repetir_ate"), "agendamento.repetir_ate"
                ),
                created_at=(
                    _parse_datetime(source.get("data_criacao"), "agendamento.data_criacao")
                    if source.get("data_criacao")
                    else starts_at
                ),
            )

        roots = [
            source
            for source in rows["agendamento"]
            if not source.get("agendamento_pai_id")
        ]
        children = [
            source
            for source in rows["agendamento"]
            if source.get("agendamento_pai_id")
        ]
        for source in roots:
            if appointment_ids[str(source["id"])] in existing_appointments:
                skipped_counts["appointments"] += 1
                continue
            db.add(appointment_entity(source))
            created_counts["appointments"] += 1
        await db.flush()
        for source in children:
            if appointment_ids[str(source["id"])] in existing_appointments:
                skipped_counts["appointments"] += 1
                continue
            db.add(appointment_entity(source))
            created_counts["appointments"] += 1
        await db.flush()

        empty_objectives = (
            literal_column("'[]'")
            if db.bind and db.bind.dialect.name == "sqlite"
            else []
        )
        for source_id, target_session_id in session_ids.items():
            if target_session_id in existing_sessions:
                skipped_counts["sessions"] += 1
                continue
            appointment = appointments_by_source[source_id]
            starts_at = _parse_datetime(
                appointment.get("dt_inicial"), "agendamento.dt_inicial"
            )
            procedure_source_id = appointment.get("procedimento_id")
            procedure = (
                procedures_by_source.get(procedure_source_id)
                if procedure_source_id
                else None
            )
            db.add(
                Session(
                    id=target_session_id,
                    patient_id=patient_ids[str(appointment["paciente_id"])],
                    professional_id=professional.id,
                    appointment_id=appointment_ids[source_id],
                    date=starts_at,
                    duration=_duration_minutes(
                        starts_at,
                        appointment.get("dt_final"),
                        service_durations.get(str(procedure_source_id), 50),
                    ),
                    type=(
                        " ".join((procedure.get("nome") or "").split())[:100]
                        if procedure
                        else "Atendimento importado"
                    ),
                    objectives=empty_objectives,
                    notes=_html_to_plain_text(appointment.get("observacao")),
                    created_at=(
                        _parse_datetime(
                            appointment.get("data_criacao"),
                            "agendamento.data_criacao",
                        )
                        if appointment.get("data_criacao")
                        else starts_at
                    ),
                )
            )
            created_counts["sessions"] += 1
        await db.flush()

        for source, audit, evolution_id in evolution_entries:
            if evolution_id in existing_evolutions:
                skipped_counts["evolutions"] += 1
                continue
            source_appointment_id = str(audit.get("agendamento_id"))
            appointment = appointments_by_source.get(source_appointment_id)
            if not appointment:
                raise LegacyClinicImportError(
                    "Há evolução apontando para agendamento ausente no arquivo"
                )
            evolution_date = _parse_datetime(
                audit.get("dt_resposta") or source.get("data_criacao"),
                "agendamento_evolucao_auditoria.dt_resposta",
            )
            content = _html_to_plain_text(source.get("resposta"))
            if not content:
                raise LegacyClinicImportError(
                    f"Evolução sem conteúdo após sanitização: {source['id']}"
                )
            db.add(
                Evolution(
                    id=evolution_id,
                    patient_id=patient_ids[str(appointment["paciente_id"])],
                    session_id=session_ids.get(source_appointment_id),
                    professional_id=professional.id,
                    date=evolution_date,
                    title="Evolução importada",
                    content=content,
                    created_at=(
                        _parse_datetime(
                            source.get("data_criacao"),
                            "agendamento_evolucao.data_criacao",
                        )
                        if source.get("data_criacao")
                        else evolution_date
                    ),
                )
            )
            created_counts["evolutions"] += 1

        await db.commit()
    except Exception:
        await db.rollback()
        pending_manifest_path.unlink(missing_ok=True)
        raise

    verified_counts = {
        "appointments": len(
            await _existing_ids(db, Appointment, list(appointment_ids.values()))
        ),
        "caregivers": len(
            await _existing_ids(db, Caregiver, [entry[3] for entry in caregiver_entries])
        ),
        "evolutions": len(
            await _existing_ids(db, Evolution, [entry[2] for entry in evolution_entries])
        ),
        "patients": len(
            await _existing_ids(db, Patient, list(patient_ids.values()))
        ),
        "services": len(
            await _existing_ids(db, ServiceOffering, list(service_ids.values()))
        ),
        "sessions": len(
            await _existing_ids(db, Session, list(session_ids.values()))
        ),
    }
    if verified_counts != expected_counts:
        raise LegacyClinicImportError(
            "A transação foi confirmada, mas a verificação pós-commit encontrou divergências"
        )
    try:
        pending_manifest_path.replace(resolved_manifest_path)
    except OSError as exc:
        raise LegacyClinicImportError(
            "Os dados foram confirmados, mas o manifesto local ficou pendente"
        ) from exc

    return LegacyClinicImportResult(
        professional_id=professional.id,
        source_sha256=source_sha256,
        manifest_path=str(resolved_manifest_path),
        created_counts=created_counts,
        skipped_counts=skipped_counts,
        verified_counts=verified_counts,
    )
