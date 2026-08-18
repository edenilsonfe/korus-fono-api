"""Safe import planning for the supported legacy clinic SQL export."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AVATAR_COLORS
from app.models.appointment import Appointment
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.finance import ServiceOffering
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


class LegacyClinicImportError(ValueError):
    """Raised when a legacy export cannot be imported safely."""


@dataclass(frozen=True)
class LegacyClinicImportPreview:
    professional_id: UUID
    source_sha256: str
    source_counts: dict[str, int]
    projected_counts: dict[str, int]
    appointment_status_counts: dict[str, int]
    warnings: list[str]


@dataclass(frozen=True)
class LegacyClinicImportResult:
    professional_id: UUID
    source_sha256: str
    created_counts: dict[str, int]


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
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
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
        source_state = appointment.get("estado_agenda_id")
        try:
            target_status = INFERRED_APPOINTMENT_STATE_MAP[str(source_state)]
        except KeyError as exc:
            raise LegacyClinicImportError(
                f"Estado de agenda sem mapeamento seguro: {source_state}"
            ) from exc
        counts[target_status] += 1
    return counts


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
        for appointment in rows["agendamento"]
    )

    return LegacyClinicImportPreview(
        professional_id=professional.id,
        source_sha256=source_sha256,
        source_counts={table: len(rows[table]) for table in SUPPORTED_TABLES},
        projected_counts={
            "appointments": len(rows["agendamento"]),
            "caregivers": caregiver_count,
            "evolutions": active_evolution_count,
            "patients": len(rows["pacientes"]),
            "services": len(rows["procedimento"]),
            "sessions": session_count,
        },
        appointment_status_counts=dict(sorted(appointment_status_counts.items())),
        warnings=[
            "O mapeamento dos estados da agenda foi inferido e exige aceite explícito para aplicar."
        ],
    )


async def apply_legacy_clinic_import(
    db: AsyncSession,
    *,
    source_path: Path,
    professional_email: str,
    accept_inferred_state_map: bool,
) -> LegacyClinicImportResult:
    """Apply a validated import after explicit acceptance of inferred states."""

    if not accept_inferred_state_map:
        raise LegacyClinicImportError(
            "Confirme explicitamente o mapeamento inferido dos estados da agenda"
        )
    professional = await _resolve_professional(db, professional_email)
    source_sha256, rows = _parse_legacy_sql(Path(source_path))
    _validate_and_count_statuses(rows["agendamento"])
    namespace = _import_namespace(professional.id, rows)

    patients_by_source = {row["id"]: row for row in rows["pacientes"]}
    procedures_by_source = {row["id"]: row for row in rows["procedimento"]}
    appointments_by_source = {row["id"]: row for row in rows["agendamento"]}
    if any(
        appointment.get("paciente_id") not in patients_by_source
        for appointment in rows["agendamento"]
    ):
        raise LegacyClinicImportError("Há agendamento apontando para paciente ausente no arquivo")
    if any(
        appointment.get("procedimento_id")
        and appointment.get("procedimento_id") not in procedures_by_source
        for appointment in rows["agendamento"]
    ):
        raise LegacyClinicImportError("Há agendamento apontando para procedimento ausente no arquivo")

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
    service_ids = {
        source_id: _target_uuid(namespace, "service", source_id)
        for source_id in procedures_by_source
    }
    appointment_ids = {
        source_id: _target_uuid(namespace, "appointment", source_id)
        for source_id in appointments_by_source
    }
    session_ids = {
        source_id: _target_uuid(namespace, "session", source_id)
        for source_id, appointment in appointments_by_source.items()
        if appointment.get("estado_agenda_id") == "4"
    }

    created_counts = {
        "appointments": 0,
        "caregivers": 0,
        "evolutions": 0,
        "patients": 0,
        "services": 0,
        "sessions": 0,
    }

    try:
        for index, patient in enumerate(rows["pacientes"]):
            source_id = str(patient["id"])
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

        for patient in rows["pacientes"]:
            source_patient_id = str(patient["id"])
            for index, spec in enumerate(_caregiver_specs(patient)):
                db.add(
                    Caregiver(
                        id=_target_uuid(
                            namespace, "caregiver", f"{source_patient_id}:{index}"
                        ),
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

        service_durations: dict[str, int] = {}
        for procedure in rows["procedimento"]:
            source_id = str(procedure["id"])
            duration = _procedure_duration(procedure.get("duracao"))
            service_durations[source_id] = duration
            price_cents = _money_to_cents(
                procedure.get("valor_total") or procedure.get("valor"),
                "procedimento.valor_total",
            )
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
            procedure = procedures_by_source.get(procedure_source_id) if procedure_source_id else None
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
                status=INFERRED_APPOINTMENT_STATE_MAP[str(source.get("estado_agenda_id"))],
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
            db.add(appointment_entity(source))
            created_counts["appointments"] += 1
        await db.flush()
        for source in children:
            db.add(appointment_entity(source))
            created_counts["appointments"] += 1
        await db.flush()

        empty_objectives = (
            literal_column("'[]'")
            if db.bind and db.bind.dialect.name == "sqlite"
            else []
        )
        for source_id, target_session_id in session_ids.items():
            appointment = appointments_by_source[source_id]
            starts_at = _parse_datetime(
                appointment.get("dt_inicial"), "agendamento.dt_inicial"
            )
            procedure_source_id = appointment.get("procedimento_id")
            procedure = procedures_by_source.get(procedure_source_id) if procedure_source_id else None
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

        audits_by_id = {
            audit["id"]: audit
            for audit in rows["agendamento_evolucao_auditoria"]
            if _is_true(audit.get("ativo"))
        }
        for source in rows["agendamento_evolucao"]:
            audit = audits_by_id.get(source.get("evolucao_auditoria_id"))
            if not audit:
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
                    id=_target_uuid(namespace, "evolution", str(source["id"])),
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
        raise

    return LegacyClinicImportResult(
        professional_id=professional.id,
        source_sha256=source_sha256,
        created_counts=created_counts,
    )
