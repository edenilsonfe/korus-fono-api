"""Reviewable CSV import; no billing, completion handlers or messaging side effects."""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AVATAR_COLORS
from app.models.anamnese import AnamneseEntry
from app.models.appointment import Appointment
from app.models.attachment import Attachment
from app.models.caregiver import Caregiver
from app.models.evolution import Evolution
from app.models.google_calendar import GoogleCalendarConnection
from app.models.notification_settings import NotificationSettings
from app.models.patient import Patient
from app.models.professional import Professional
from app.models.session import Session
from app.models.timeline import TimelineEvent
from app.services.legacy_csv_source import (
    CLINICAL_TABLES,
    IDS,
    CsvImportError,
    CsvSource,
    address,
    birth_date,
    bounded,
    clinical_content,
    clinical_date,
    digest,
    history_pdf,
    local_datetime,
    normalized,
    plain,
    source_registration,
    text_value,
)

VERSION = 1
SOURCE_SYSTEM = "clinic_csv_v1"
MODELS = {
    m.__tablename__: m
    for m in (
        Patient,
        Caregiver,
        Appointment,
        Session,
        AnamneseEntry,
        Evolution,
        Attachment,
        TimelineEvent,
    )
}
INSERT_ORDER = tuple(MODELS)
PROVENANCE = "Histórico importado. Situação concluída definida para a migração pelo cliente; situação não exportada pela origem."


def json_value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    if isinstance(value, (date, time, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def fingerprint(value) -> str:
    return digest(
        json.dumps(
            json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(json_value(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise CsvImportError("Manifesto ilegível") from exc
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise CsvImportError("Manifesto incompatível")
    return value


@dataclass(repr=False)
class TargetSnapshot:
    professional: dict = field(repr=False)
    rows: dict[str, list[dict]] = field(repr=False)
    whatsapp_enabled: bool = False
    google_connections: int = 0

    @property
    def sha256(self):
        return fingerprint(
            {
                "professional": self.professional,
                "rows": self.rows,
                "whatsapp": self.whatsapp_enabled,
                "google": self.google_connections,
            }
        )


async def snapshot(db: AsyncSession, email: str, *, lock=False) -> TargetSnapshot:
    statement = select(
        Professional.id, Professional.email, Professional.name, Professional.is_disabled
    ).where(func.lower(func.trim(Professional.email)) == email.strip().lower())
    if lock:
        statement = statement.with_for_update()
    matches = (await db.execute(statement)).mappings().all()
    if len(matches) != 1 or matches[0]["is_disabled"]:
        raise CsvImportError("Conta de destino não é única ou está desabilitada")
    professional = dict(matches[0])
    pid = professional["id"]
    result = {}
    for table_name, model in MODELS.items():
        table = model.__table__
        query = select(table)
        if "professional_id" in table.c:
            query = query.where(table.c.professional_id == pid)
        else:
            query = query.where(
                table.c.patient_id.in_(
                    select(Patient.id).where(Patient.professional_id == pid)
                )
            )
        query = query.order_by(table.c.id)
        if lock:
            query = query.with_for_update()
        result[table_name] = [
            dict(r) for r in (await db.execute(query)).mappings().all()
        ]
    whatsapp = await db.scalar(
        select(NotificationSettings.whatsapp_enabled).where(
            NotificationSettings.professional_id == pid
        )
    )
    google = await db.scalar(
        select(func.count())
        .select_from(GoogleCalendarConnection)
        .where(GoogleCalendarConnection.professional_id == pid)
    )
    return TargetSnapshot(professional, result, bool(whatsapp), google or 0)


@dataclass(repr=False)
class Record:
    table: str
    source_id: str
    values: dict = field(repr=False)
    action: str = "create"
    changes: dict = field(default_factory=dict, repr=False)

    def manifest(self):
        return {
            "table": self.table,
            "sourceId": self.source_id,
            "id": str(self.values["id"]),
            "fields": sorted(self.values),
            "sha256": fingerprint(self.values),
            "action": self.action,
        }


@dataclass(repr=False)
class Plan:
    report: dict
    records: list[Record] = field(repr=False)
    objects: dict[str, bytes] = field(repr=False)
    baseline: TargetSnapshot = field(repr=False)
    source: CsvSource = field(repr=False)


def build_plan(
    source: CsvSource,
    target: TargetSnapshot,
    *,
    patient_matches: dict[str, str | dict] | None = None,
) -> Plan:
    professional_id = target.professional["id"]
    namespace = uuid5(
        NAMESPACE_URL, f"korus/{SOURCE_SYSTEM}/{professional_id}/{source.source_key}"
    )

    def uid(table, source_id):
        return uuid5(namespace, f"{table}/{source_id}")

    existing = {
        table: {r["id"]: r for r in rows} for table, rows in target.rows.items()
    }
    by_name = defaultdict(list)
    for p in target.rows["patients"]:
        if not p["is_demo"]:
            by_name[normalized(p["name"])].append(p)
    patient_matches = patient_matches or {}
    source_patients = {r["id_paciente"]: r for r in source.tables["pacientes"]}
    if not set(patient_matches).issubset(source_patients):
        raise CsvImportError("Mapeamento contém paciente fora do lote")
    records, objects, patient_ids, links = [], {}, {}, []
    claimed = set()
    counts = Counter()
    agendas = defaultdict(list)
    historical_dates = defaultdict(list)
    for row in source.tables["agenda"]:
        agendas[row["id_paciente"]].append(row)
        historical_dates[row["id_paciente"]].append(
            local_datetime(row["inicio"], "agenda.inicio", source.zone).date()
        )
    for table in CLINICAL_TABLES:
        for row in source.tables[table]:
            historical_dates[row["id_paciente"]].append(
                clinical_date(table, row, source.zone).astimezone(source.zone).date()
            )

    def add(table, sid, values, action="create", changes=None):
        record = Record(table, sid, values, action, changes or {})
        if action == "create" and values["id"] in existing[table]:
            raise CsvImportError("ID determinístico já existe sem manifesto do lote")
        records.append(record)
        return record

    def timeline(sid, patient_id, event_type, title, at, source_id, description=""):
        add(
            "timeline_events",
            sid,
            {
                "id": uid("timeline", sid),
                "patient_id": patient_id,
                "professional_id": professional_id,
                "type": event_type,
                "title": title,
                "description": description,
                "date": at,
                "source_id": source_id,
            },
        )

    for index, row in enumerate(source.tables["pacientes"]):
        sid = row["id_paciente"]
        birth = birth_date(row["data_nascimento"])
        candidates = by_name[normalized(row["nome"])]
        match = None
        birth_resolution = None
        if sid in patient_matches:
            reviewed = patient_matches[sid]
            target_ref = (
                reviewed.get("targetId") if isinstance(reviewed, dict) else reviewed
            )
            try:
                match = existing["patients"].get(UUID(target_ref))
            except (ValueError, TypeError, AttributeError) as exc:
                raise CsvImportError("ID inválido no vínculo manual") from exc
            if (
                match is None
                or match["is_demo"]
                or normalized(match["name"]) != normalized(row["nome"])
            ):
                raise CsvImportError(
                    "Vínculo manual não corresponde à identidade da origem e da conta"
                )
            if isinstance(reviewed, dict):
                birth_resolution = reviewed.get("birthDateResolution")
                if (
                    reviewed.get("sourceBirthDate") != str(birth)
                    or reviewed.get("targetBirthDate") != str(match["birth_date"])
                    or birth_resolution not in {"keep_target", "use_source"}
                ):
                    raise CsvImportError(
                        "Revisão de nascimento divergente ou incompleta"
                    )
            elif match["birth_date"] != birth:
                raise CsvImportError(
                    "Nascimento divergente exige decisão explícita revisada"
                )
        elif candidates:
            exact = [p for p in candidates if p["birth_date"] == birth]
            if len(candidates) != 1 or len(exact) != 1:
                raise CsvImportError(
                    "Correspondência ambígua de paciente; revise nomes e nascimentos localmente"
                )
            match = exact[0]
        target_id = match["id"] if match else uid("patients", sid)
        if target_id in claimed:
            raise CsvImportError(
                "Dois pacientes da origem apontam para o mesmo destino"
            )
        claimed.add(target_id)
        patient_ids[sid] = target_id
        addr, notes = (
            address(row),
            bounded(plain(row.get("obs")), 5000, "notes") or None,
        )
        counts["addresses"] += bool(addr)
        counts["notes"] += bool(notes)
        if match:
            changes = {}
            if birth_resolution == "use_source" and match["birth_date"] != birth:
                changes["birth_date"] = birth
            for key, value in (("address", addr), ("notes", notes)):
                if value and match.get(key) and match[key] != value:
                    raise CsvImportError(
                        f"Campo {key} já preenchido com outro valor no paciente correspondente"
                    )
                if value and not match.get(key):
                    changes[key] = value
            if match["status"] != "ativo":
                changes["status"] = "ativo"
            values = {k: v for k, v in match.items() if k != "updated_at"}
            values.update(changes)
            add("patients", sid, values, "update" if changes else "preserve", changes)
            links.append(
                {
                    "sourceId": sid,
                    "targetId": str(target_id),
                    "rule": "reviewed_birth_conflict"
                    if birth_resolution
                    else "unique_normalized_name_and_birth",
                    **(
                        {"birthDateResolution": birth_resolution}
                        if birth_resolution
                        else {}
                    ),
                }
            )
        else:
            created = local_datetime(
                row["created_at"], "pacientes.created_at", source.zone
            ).astimezone(UTC)
            add(
                "patients",
                sid,
                {
                    "id": target_id,
                    "professional_id": professional_id,
                    "name": text_value(row["nome"]),
                    "birth_date": birth,
                    "address": addr,
                    "notes": notes,
                    "diagnosis_keys": ["nao_informado"],
                    "status": "ativo",
                    "start_date": min(historical_dates[sid])
                    if historical_dates[sid]
                    else created.astimezone(source.zone).date(),
                    "avatar_color": AVATAR_COLORS[index % len(AVATAR_COLORS)],
                    "is_demo": False,
                    "created_at": created,
                    "therapy_plan_content": None,
                    "therapy_plan_updated_at": None,
                    "anamnese_status": "draft",
                    "anamnese_completed_at": None,
                },
            )
        for key, relation in (("nome_mae", "Mãe"), ("nome_pai", "Pai")):
            name = bounded(text_value(row.get(key)), 255, key)
            if not name:
                continue
            duplicates = [
                c
                for c in target.rows["caregivers"]
                if c["patient_id"] == target_id
                and normalized(c["name"]) == normalized(name)
                and normalized(c["relation"]) == normalized(relation)
            ]
            if len(duplicates) > 1:
                raise CsvImportError("Responsável correspondente duplicado no destino")
            caregiver_sid = f"{sid}/{key}"
            if duplicates:
                add("caregivers", caregiver_sid, duplicates[0], "preserve")
            else:
                add(
                    "caregivers",
                    caregiver_sid,
                    {
                        "id": uid("caregivers", caregiver_sid),
                        "patient_id": target_id,
                        "name": name,
                        "relation": relation,
                        "phone": "",
                        "email": "",
                        "notes": "Parentesco informado no cadastro de origem; contato não atribuído automaticamente.",
                        "is_primary": False,
                        "whatsapp_opt_in": False,
                    },
                )

    for row in source.tables["agenda"]:
        sid = row["id_agenda"]
        patient_id = patient_ids[row["id_paciente"]]
        start = local_datetime(row["inicio"], "agenda.inicio", source.zone)
        end = local_datetime(row["fim"], "agenda.fim", source.zone)
        if start.date() >= datetime.now(source.zone).date():
            raise CsvImportError("O lote histórico contém agenda atual ou futura")
        appointment_id, session_id = uid("appointments", sid), uid("sessions", sid)
        kind = text_value(row["procedimento"])
        duration = int((end - start).total_seconds() / 60)
        add(
            "appointments",
            sid,
            {
                "id": appointment_id,
                "patient_id": patient_id,
                "professional_id": professional_id,
                "date": start.date(),
                "time": start.time().replace(tzinfo=None),
                "duration": duration,
                "type": kind,
                "status": "concluido",
                "service_id": None,
                "service_name_snapshot": kind,
                "service_price_cents": None,
                "appointment_type": "avulso",
                "series_id": None,
                "frequency": None,
                "end_date": None,
                "weekdays": None,
                "weekday_slots": None,
            },
        )
        add(
            "sessions",
            sid,
            {
                "id": session_id,
                "patient_id": patient_id,
                "professional_id": professional_id,
                "appointment_id": appointment_id,
                "date": start.astimezone(UTC),
                "duration": duration,
                "type": kind,
                "objectives": [],
                "notes": PROVENANCE,
            },
        )
        timeline(
            f"session/{sid}",
            patient_id,
            "sessao",
            "Sessão histórica importada",
            start.astimezone(UTC),
            session_id,
            PROVENANCE,
        )

    for table in CLINICAL_TABLES:
        for row in source.tables[table]:
            sid = f"{table}/{row[IDS[table]]}"
            patient_id = patient_ids[row["id_paciente"]]
            at = clinical_date(table, row, source.zone)
            content = clinical_content(table, row)
            existing_patient = existing["patients"].get(patient_id)
            locked = (
                existing_patient and existing_patient["anamnese_status"] == "completed"
            )
            if table == "consulta_multi" and not locked:
                section = f"Anamnese importada - {at.astimezone(source.zone).date()} - {row[IDS[table]]}"
                if any(
                    a["patient_id"] == patient_id and a["section"] == section
                    for a in target.rows["anamnese_entries"]
                ):
                    raise CsvImportError(
                        "Seção de anamnese importada já existe sem manifesto"
                    )
                record_id = uid("anamnese_entries", sid)
                add(
                    "anamnese_entries",
                    sid,
                    {
                        "id": record_id,
                        "patient_id": patient_id,
                        "section": section,
                        "value": content,
                        "created_at": local_datetime(
                            row["data_criacao"], "data_criacao", source.zone
                        ).astimezone(UTC),
                    },
                )
            else:
                title = {
                    "consulta_multi": "Ficha clínica importada",
                    "ficha_adendo": "Adendo importado",
                    "pos_operatorio": "Ficha pós-operatória importada (tipo de origem)",
                }[table]
                record_id = uid("evolutions", sid)
                add(
                    "evolutions",
                    sid,
                    {
                        "id": record_id,
                        "patient_id": patient_id,
                        "professional_id": professional_id,
                        "session_id": None,
                        "date": at,
                        "title": title,
                        "content": content,
                        "created_at": local_datetime(
                            row["data_criacao"], "data_criacao", source.zone
                        ).astimezone(UTC),
                    },
                )
                timeline(sid, patient_id, "evolucao", title, at, record_id)

    for sid, row in source_patients.items():
        patient_agenda = agendas[sid]
        if not patient_agenda and not source_registration(row):
            continue
        payload = history_pdf(row, patient_agenda, source.zone)
        patient_id = patient_ids[sid]
        attachment_id = uid("attachments", sid)
        key = f"patients/{patient_id}/imports/{namespace}/{digest(payload)}.pdf"
        objects[key] = payload
        counts["agendaPdfs" if patient_agenda else "registrationPdfs"] += 1
        at = max(
            (
                local_datetime(a["inicio"], "agenda.inicio", source.zone).astimezone(
                    UTC
                )
                for a in patient_agenda
            ),
            default=local_datetime(
                row["created_at"], "created_at", source.zone
            ).astimezone(UTC),
        )
        title = (
            "Histórico de agenda importado.pdf"
            if patient_agenda
            else "Cadastro de origem.pdf"
        )
        add(
            "attachments",
            sid,
            {
                "id": attachment_id,
                "patient_id": patient_id,
                "professional_id": professional_id,
                "name": title,
                "category": "relatorio",
                "storage_key": key,
                "size_bytes": len(payload),
                "date": at,
            },
        )

    record_ids = {(r.table, r.values["id"]) for r in records}
    if len(record_ids) != len(records):
        raise CsvImportError("Plano contém IDs repetidos")
    preserved = []
    for table, rows in target.rows.items():
        for row in rows:
            if (table, row["id"]) not in record_ids:
                preserved.append(Record(table, "existing", row, "preserve"))
    records.extend(preserved)
    policy = {
        "patientStatus": "ativo",
        "appointmentStatus": "concluido",
        "statusRule": "user_confirmed_completed",
        "excludedIds": source.excluded,
        "timezone": source.timezone,
        "patientMatches": patient_matches,
    }
    report = {
        "version": VERSION,
        "sourceSystem": SOURCE_SYSTEM,
        "sourceKey": source.source_key,
        "professionalId": str(professional_id),
        "professionalEmail": target.professional["email"],
        "sourceSha256": source.source_sha256,
        "sourceHashes": source.hashes,
        "sourceCounts": source.source_counts,
        "policy": policy,
        "policySha256": fingerprint(policy),
        "baselineSha256": target.sha256,
        "existingCounts": {table: len(rows) for table, rows in target.rows.items()},
        "includedPatients": len(source_patients),
        "matchedPatients": links,
        "createdCounts": dict(
            Counter(r.table for r in records if r.action == "create")
        ),
        "updatedCounts": dict(
            Counter(r.table for r in records if r.action == "update")
        ),
        "preservedCounts": dict(
            Counter(r.table for r in records if r.action == "preserve")
        ),
        "contentCounts": dict(counts),
        "quoteRepairs": source.repairs,
        "objects": [
            {"key": k, "sha256": digest(v), "sizeBytes": len(v)}
            for k, v in sorted(objects.items())
        ],
        "records": [r.manifest() for r in records],
        "blockers": (
            ["WhatsApp deve permanecer desabilitado durante a migração"]
            if target.whatsapp_enabled
            else []
        )
        + (
            ["Revise a conexão ativa do Google Agenda antes da migração"]
            if target.google_connections
            else []
        ),
    }
    report["previewSha256"] = fingerprint(report)
    return Plan(report, records, objects, target, source)


async def preview(
    db: AsyncSession, source: CsvSource, email: str, *, patient_matches=None
) -> Plan:
    if db.in_transaction():
        raise CsvImportError("A prévia exige sessão dedicada sem transação anterior")
    try:
        if db.bind.dialect.name == "postgresql":
            await db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
        target = await snapshot(db, email)
        return build_plan(source, target, patient_matches=patient_matches)
    finally:
        await db.rollback()


async def verify_records(db: AsyncSession, manifest: dict) -> dict:
    counts = Counter()
    grouped = defaultdict(list)
    for spec in manifest["records"]:
        if spec["table"] not in MODELS:
            raise CsvImportError("Tabela inválida no manifesto")
        grouped[spec["table"]].append(spec)
    for table, specs in grouped.items():
        model = MODELS[table]
        found = {}
        for start in range(0, len(specs), 200):
            ids = [UUID(s["id"]) for s in specs[start : start + 200]]
            rows = (
                (await db.execute(select(model.__table__).where(model.id.in_(ids))))
                .mappings()
                .all()
            )
            found.update({str(r["id"]): dict(r) for r in rows})
        for spec in specs:
            row = found.get(spec["id"])
            if (
                row is None
                or fingerprint({k: row[k] for k in spec["fields"]}) != spec["sha256"]
            ):
                raise CsvImportError(
                    f"Registro ausente ou modificado no destino: {table}/{spec['id']}"
                )
            if spec["action"] == "create":
                counts[table] += 1
    return dict(counts)


async def verify_objects(storage, manifest: dict):
    for obj in manifest["objects"]:
        payload = await storage.read(obj["key"])
        if payload is None or digest(payload) != obj["sha256"]:
            raise CsvImportError("PDF ausente ou modificado no storage")


def validate_manifest(
    manifest: dict, source: CsvSource, professional_id: UUID, policy: dict
):
    if (
        manifest.get("sourceSystem") != SOURCE_SYSTEM
        or manifest.get("professionalId") != str(professional_id)
        or manifest.get("sourceSha256") != source.source_sha256
        or manifest.get("policySha256") != fingerprint(policy)
    ):
        raise CsvImportError(
            "Manifesto não corresponde à conta, aos arquivos ou às regras atuais"
        )
    report = {k: v for k, v in manifest.items() if k not in {"previewSha256", "state"}}
    if fingerprint(report) != manifest.get("previewSha256"):
        raise CsvImportError("Manifesto alterado ou incompleto")


async def apply(
    db: AsyncSession,
    source: CsvSource,
    email: str,
    *,
    expected_preview_sha256: str,
    manifest_path: Path,
    storage,
    backup,
    patient_matches=None,
) -> dict:
    """Apply only a reviewed fingerprint. Backup is a caller-supplied encrypted sink."""
    if db.in_transaction() or not expected_preview_sha256:
        raise CsvImportError("Aplicação exige sessão dedicada e hash da prévia")
    pending_path = manifest_path.with_name(manifest_path.name + ".pending")
    complete, pending = load_manifest(manifest_path), load_manifest(pending_path)
    if complete and pending:
        raise CsvImportError(
            "Manifestos final e pendente coexistem; revise antes de continuar"
        )
    policy = {
        "patientStatus": "ativo",
        "appointmentStatus": "concluido",
        "statusRule": "user_confirmed_completed",
        "excludedIds": source.excluded,
        "timezone": source.timezone,
        "patientMatches": patient_matches or {},
    }
    report = None
    try:
        async with db.begin():
            if db.bind.dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            target = await snapshot(db, email, lock=True)
            manifest = complete or pending
            if manifest:
                validate_manifest(manifest, source, target.professional["id"], policy)
                if manifest["previewSha256"] != expected_preview_sha256:
                    raise CsvImportError("Hash solicitado diverge do manifesto")
                created_specs = [
                    s for s in manifest["records"] if s["action"] == "create"
                ]
                presence = 0
                for table, model in MODELS.items():
                    ids = [UUID(s["id"]) for s in created_specs if s["table"] == table]
                    for start in range(0, len(ids), 200):
                        presence += (
                            await db.scalar(
                                select(func.count())
                                .select_from(model)
                                .where(model.id.in_(ids[start : start + 200]))
                            )
                            or 0
                        )
                if complete or presence:
                    if presence != len(created_specs):
                        raise CsvImportError(
                            "O lote existe parcialmente no destino; recuperação automática bloqueada"
                        )
                    verified = await verify_records(db, manifest)
                    await verify_objects(storage, manifest)
                    if pending:
                        save_json(manifest_path, {**manifest, "state": "complete"})
                        pending_path.unlink()
                    return {
                        "status": "already_applied",
                        "verifiedCounts": verified,
                        "previewSha256": expected_preview_sha256,
                    }
            plan = build_plan(source, target, patient_matches=patient_matches)
            report = plan.report
            if report["blockers"]:
                raise CsvImportError("; ".join(report["blockers"]))
            if report["previewSha256"] != expected_preview_sha256:
                raise CsvImportError(
                    "Arquivos, regras ou destino mudaram após a prévia; gere uma nova prévia"
                )
            await storage.check()
            backup(target, expected_preview_sha256)
            save_json(pending_path, {**report, "state": "pending"})
            semaphore = asyncio.Semaphore(4)

            async def upload(key, payload):
                async with semaphore:
                    await storage.put_verified(key, payload)

            uploads = await asyncio.gather(
                *(upload(k, v) for k, v in plan.objects.items()), return_exceptions=True
            )
            if any(isinstance(x, BaseException) for x in uploads):
                raise CsvImportError(
                    "Upload incompleto; banco não alterado e manifesto mantido para recuperação"
                )
            for table in INSERT_ORDER:
                for record in plan.records:
                    if record.table != table:
                        continue
                    if record.action == "create":
                        db.add(MODELS[table](**record.values))
                    elif record.action == "update":
                        await db.execute(
                            update(MODELS[table])
                            .where(MODELS[table].id == record.values["id"])
                            .values(**record.changes)
                        )
                await db.flush()
            await verify_records(db, report)
        # Read back after the transaction has committed; failures keep .pending.
        verified = await verify_records(db, report)
        await verify_objects(storage, report)
        await db.rollback()
        save_json(manifest_path, {**report, "state": "complete"})
        pending_path.unlink()
        return {
            "status": "applied",
            "createdCounts": report["createdCounts"],
            "updatedCounts": report["updatedCounts"],
            "verifiedCounts": verified,
            "previewSha256": expected_preview_sha256,
        }
    except Exception:
        await db.rollback()
        raise
