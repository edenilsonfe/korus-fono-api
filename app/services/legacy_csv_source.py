"""Strict, non-executing reader for the supported clinic CSV export.

Clinical text stays in memory. Public reports contain counts, IDs and hashes only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from app.services.legacy_clinic_import import _html_to_plain_text

FILES = (
    "pacientes",
    "agenda",
    "consulta_multi",
    "ficha_adendo",
    "pos_operatorio",
    "contratante",
    "contratante_usuario",
    "exame",
    "prontuarios_dav",
)
IDS = {
    "pacientes": "id_paciente",
    "agenda": "id_agenda",
    "consulta_multi": "id_consulta_mult",
    "ficha_adendo": "id_ficha_adendo",
    "pos_operatorio": "id_pos_operatorio",
    "contratante": "id_contratante",
    "contratante_usuario": "id_contratante_usuario",
    "exame": "id_exame",
}
REQUIRED = {
    "pacientes": {
        "id_paciente",
        "id_contratante",
        "nome",
        "data_nascimento",
        "created_at",
        "obs",
        "endereco",
    },
    "agenda": {
        "id_agenda",
        "id_paciente",
        "paciente",
        "profissional",
        "inicio",
        "fim",
        "procedimento",
    },
    "consulta_multi": {
        "id_consulta_mult",
        "id_paciente",
        "id_contratante_usuario",
        "conteudo",
        "historico",
        "data_criacao",
        "data_prontuario",
        "fl_status",
        "dados_medicos",
    },
    "ficha_adendo": {
        "id_ficha_adendo",
        "id_paciente",
        "id_contratante_usuario",
        "data",
        "conduta",
        "data_criacao",
        "fl_status",
        "dados_medicos",
    },
    "pos_operatorio": {
        "id_pos_operatorio",
        "id_paciente",
        "id_contratante_usuario",
        "data",
        "anamnese",
        "conduta",
        "exame_fisico",
        "data_criacao",
        "fl_status",
        "dados_medicos",
    },
}
CLINICAL_TABLES = ("consulta_multi", "ficha_adendo", "pos_operatorio")
ADMIN_PREFIXES = {
    "contratante": [
        "id_contratante",
        "seq",
        "cd_pf_pj",
        "nome",
        "responsavel",
        "email",
    ],
    "contratante_usuario": [
        "id_contratante_usuario",
        "seq",
        "id_contratante",
        "nome",
        "nome_completo",
    ],
}


class CsvImportError(ValueError):
    """Messages must not contain patient names, contact data or clinical text."""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(c for c in value if not unicodedata.combining(c)).casefold().split()
    )


def text_value(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.lower() in {"null", "none", "\\n"} else value


def plain(value: str | None) -> str:
    return _html_to_plain_text(text_value(value))


def bounded(value: str, limit: int, label: str) -> str:
    if len(value) > limit or "\x00" in value:
        raise CsvImportError(f"Campo inválido ou acima do limite: {label}")
    return value


def local_datetime(value: str, label: str, zone: ZoneInfo) -> datetime:
    try:
        result = datetime.fromisoformat(text_value(value))
    except ValueError as exc:
        raise CsvImportError(f"Data inválida em {label}") from exc
    return (
        result.replace(tzinfo=zone)
        if result.tzinfo is None
        else result.astimezone(zone)
    )


def birth_date(value: str) -> date:
    try:
        result = date.fromisoformat(text_value(value))
    except ValueError as exc:
        raise CsvImportError("Nascimento ausente ou inválido") from exc
    if result > datetime.now(UTC).date():
        raise CsvImportError("Nascimento futuro")
    return result


def address(row: dict[str, str]) -> str | None:
    street = ", ".join(
        text_value(row.get(k)) for k in ("endereco", "numero") if text_value(row.get(k))
    )
    city = "/".join(
        text_value(row.get(k)) for k in ("cidade", "estado") if text_value(row.get(k))
    )
    parts = [
        street,
        text_value(row.get("complemento")),
        text_value(row.get("bairro")),
        city,
    ]
    if text_value(row.get("cep")):
        parts.append("CEP " + text_value(row["cep"]))
    return bounded(" - ".join(p for p in parts if p), 500, "address") or None


@dataclass(repr=False)
class CsvSource:
    tables: dict[str, list[dict[str, str]]] = field(repr=False)
    hashes: dict[str, str]
    source_key: str
    repairs: int
    excluded: tuple[str, ...]
    timezone: str
    source_counts: dict[str, int]

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def source_sha256(self) -> str:
        return digest(json.dumps(self.hashes, sort_keys=True).encode())


def read_source(
    directory: Path,
    *,
    excluded_ids: tuple[str, ...],
    timezone: str = "America/Sao_Paulo",
) -> CsvSource:
    csv.field_size_limit(20_000_000)
    tables, hashes, counts = {}, {}, {}
    repairs = 0
    try:
        zone = ZoneInfo(timezone)
    except Exception as exc:
        raise CsvImportError("Fuso horário inválido") from exc
    for name in FILES:
        path = directory / f"{name}.csv"
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise CsvImportError(f"Não foi possível ler {name}.csv em UTF-8") from exc
        hashes[path.name] = digest(raw)
        if "\x00" in content:
            raise CsvImportError(f"Byte nulo em {name}.csv")
        if name == "consulta_multi":
            # These exports double CSV quotes but leave JSON's backslash-quotes single.
            content, repairs = re.subn(r'\\"(?!")', lambda _: '\\""', content)
        try:
            parsed = list(csv.reader(io.StringIO(content), delimiter=";", strict=True))
        except csv.Error as exc:
            raise CsvImportError(f"CSV inválido: {name}") from exc
        if not parsed:
            raise CsvImportError(f"Cabeçalho ausente: {name}")
        header, rows = parsed[0], parsed[1:]
        if len(header) != len(set(header)):
            raise CsvImportError(f"Coluna duplicada: {name}")
        if name == "prontuarios_dav":
            if rows or len(header) != 1 or "nenhum" not in normalized(header[0]):
                raise CsvImportError("Prontuários DAV com conteúdo não suportado")
            tables[name] = []
            counts[name] = 0
            continue
        if name in ADMIN_PREFIXES:
            prefix = ADMIN_PREFIXES[name]
            if header[: len(prefix)] != prefix or any(
                len(r) < len(prefix) for r in rows
            ):
                raise CsvImportError(
                    f"Identificação administrativa não suportada: {name}"
                )
            # Never load passwords/tokens or trust shifted administrative columns.
            tables[name] = [dict(zip(prefix, r[: len(prefix)])) for r in rows]
        else:
            if not REQUIRED.get(name, {IDS[name]}).issubset(header):
                raise CsvImportError(f"Colunas obrigatórias ausentes: {name}")
            if any(len(r) != len(header) for r in rows):
                raise CsvImportError(f"Quantidade de colunas inconsistente: {name}")
            tables[name] = [dict(zip(header, r)) for r in rows]
        ids = [text_value(r[IDS[name]]) for r in tables[name]]
        if any(not i for i in ids) or len(ids) != len(set(ids)):
            raise CsvImportError(f"ID vazio ou duplicado: {name}")
        counts[name] = len(rows)
    contractors = tables["contratante"]
    if len(contractors) != 1:
        raise CsvImportError("A exportação deve conter uma única organização")
    key = contractors[0]["id_contratante"]
    all_patients = {r["id_paciente"]: r for r in tables["pacientes"]}
    excluded = set(excluded_ids)
    if not excluded.issubset(all_patients):
        raise CsvImportError("Paciente excluído não encontrado no lote")
    for table in ("agenda", *CLINICAL_TABLES):
        for row in tables[table]:
            if row["id_paciente"] not in all_patients:
                raise CsvImportError(f"Referência órfã de paciente: {table}")
            if row["id_paciente"] in excluded:
                raise CsvImportError(
                    "Paciente excluído possui histórico; revise o escopo"
                )
    tables["pacientes"] = [
        r for r in tables["pacientes"] if r["id_paciente"] not in excluded
    ]
    if not tables["pacientes"]:
        raise CsvImportError("Nenhum paciente incluído no lote")
    users = {r["id_contratante_usuario"]: r for r in tables["contratante_usuario"]}
    if any(r["id_contratante"] != key for r in [*tables["pacientes"], *users.values()]):
        raise CsvImportError("Organizações divergentes na exportação")
    seen_identity = set()
    for row in tables["pacientes"]:
        name = bounded(text_value(row["nome"]), 255, "name")
        if not name:
            raise CsvImportError("Paciente sem nome")
        identity = (normalized(name), birth_date(row["data_nascimento"]))
        if identity in seen_identity:
            raise CsvImportError("Nome e nascimento duplicados na origem")
        seen_identity.add(identity)
        local_datetime(row["created_at"], "pacientes.created_at", zone)
        address(row)
        bounded(plain(row.get("obs")), 5000, "notes")
    for row in tables["agenda"]:
        start = local_datetime(row["inicio"], "agenda.inicio", zone)
        end = local_datetime(row["fim"], "agenda.fim", zone)
        seconds = (end - start).total_seconds()
        if start.date() != end.date() or seconds <= 0 or seconds % 60:
            raise CsvImportError("Duração de agenda inválida")
        bounded(text_value(row["procedimento"]), 100, "agenda.procedimento")
        if not text_value(row["procedimento"]):
            raise CsvImportError("Procedimento da agenda vazio")
        if normalized(row["paciente"]) != normalized(
            all_patients[row["id_paciente"]]["nome"]
        ):
            raise CsvImportError("Nome da agenda diverge do cadastro vinculado")
        if normalized(row["profissional"]) not in {
            normalized(r["nome"]) for r in users.values()
        }:
            raise CsvImportError("Profissional da agenda não encontrado")
    for table in CLINICAL_TABLES:
        for row in tables[table]:
            if row["id_contratante_usuario"] not in users or row["fl_status"] != "PR":
                raise CsvImportError(
                    f"Autoria ou situação clínica não suportada: {table}"
                )
            try:
                medical = json.loads(row["dados_medicos"])
            except (ValueError, TypeError) as exc:
                raise CsvImportError(f"Autoria clínica inválida: {table}") from exc
            if (
                not isinstance(medical, dict)
                or medical.get("id_contratante_usuario")
                != row["id_contratante_usuario"]
            ):
                raise CsvImportError(f"Autoria clínica divergente: {table}")
            clinical_content(table, row)
            clinical_date(table, row, zone)
            local_datetime(row["data_criacao"], f"{table}.data_criacao", zone)
    return CsvSource(
        tables, hashes, key, repairs, tuple(sorted(excluded)), timezone, counts
    )


def clinical_date(table: str, row: dict, zone: ZoneInfo) -> datetime:
    preferred = "data_prontuario" if table == "consulta_multi" else "data"
    value = text_value(row.get(preferred)) or text_value(row.get("data_criacao"))
    return local_datetime(value, f"{table}.{preferred}", zone).astimezone(UTC)


def clinical_content(table: str, row: dict) -> str:
    medical = json.loads(row["dados_medicos"])
    parts = [
        "Registro importado do sistema anterior.",
        "Autoria na origem: " + text_value(medical.get("nome")),
    ]
    for key in ("data", "data_criacao", "data_prontuario"):
        if text_value(row.get(key)):
            parts.append(f"{key}: {row[key]}")
    parts.append(
        "Situação na origem: "
        + row["fl_status"]
        + ". Assinatura digital não verificada pela importação."
    )
    if table == "consulta_multi":
        try:
            fields = json.loads(row["conteudo"])
        except ValueError as exc:
            raise CsvImportError("JSON de ficha clínica inválido") from exc
        if not isinstance(fields, list) or not fields:
            raise CsvImportError("Ficha clínica sem campos")
        for item in fields:
            if not isinstance(item, dict) or not isinstance(item.get("label"), str):
                raise CsvImportError("Componente de ficha inválido")
            label = plain(item["label"])
            parts.append("\n" + label)
            if item.get("type") == "header":
                continue
            values = item.get("userData", [])
            if not isinstance(values, list):
                raise CsvImportError("Resposta da ficha não é uma lista")
            options = {
                str(v.get("value")): str(v.get("label", v.get("value", "")))
                for v in item.get("values", [])
                if isinstance(v, dict)
            }
            answers = []
            for value in values:
                if isinstance(value, (dict, list)):
                    raise CsvImportError("Resposta estruturada não suportada na ficha")
                original = text_value(str(value)) if value is not None else ""
                if original:
                    answers.append(plain(options.get(original, original)))
            parts.append("; ".join(answers) or "Não preenchido na origem.")
        if plain(row.get("historico")):
            parts.extend(["\nHistórico de origem", plain(row["historico"])])
    else:
        keys = (
            ("conduta",)
            if table == "ficha_adendo"
            else ("anamnese", "exame_fisico", "conduta")
        )
        if not any(plain(row.get(k)) for k in keys):
            raise CsvImportError(f"Registro clínico vazio: {table}")
        for key in keys:
            if plain(row.get(key)):
                parts.extend(
                    ["\n" + key.replace("_", " ").capitalize(), plain(row[key])]
                )
    return "\n".join(parts)


def source_registration(row: dict) -> list[tuple[str, str]]:
    keys = {
        "telefone": "Telefone (titular não informado)",
        "celular": "Celular (titular não informado)",
        "email": "E-mail (titular não informado)",
        "cpf": "CPF informado na origem",
        "sexo": "Sexo na origem",
        "convenio": "Convênio",
        "numero_convenio": "Número do convênio",
        "outro_doc": "Outro documento",
        "nome_mae": "Mãe",
        "nome_pai": "Pai",
    }
    return [
        (label, text_value(row.get(k)))
        for k, label in keys.items()
        if text_value(row.get(k))
    ]


def history_pdf(patient: dict, appointments: list[dict], zone: ZoneInfo) -> bytes:
    """Deterministic private document, built in memory without temporary PHI files."""
    stream = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="SmallKorus",
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            spaceAfter=3,
        )
    )
    small = styles["SmallKorus"]

    def paragraph(value, style=small):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    title = "Histórico de agenda importado" if appointments else "Cadastro de origem"
    flow = [
        paragraph("KORUSFONO", styles["Heading2"]),
        paragraph(title, styles["Title"]),
        paragraph(patient["nome"], styles["Heading2"]),
        paragraph(
            "Nascimento: " + birth_date(patient["data_nascimento"]).strftime("%d/%m/%Y")
        ),
        paragraph(
            "Dados transcritos do sistema anterior. Contatos e dados cadastrais não foram revalidados pela importação."
        ),
    ]
    registration = source_registration(patient)
    if address(patient):
        registration.append(("Endereço", address(patient)))
    if plain(patient.get("obs")):
        registration.append(("Observações", plain(patient["obs"])))
    if registration:
        flow.append(paragraph("Cadastro de origem", styles["Heading2"]))
        for label, value in registration:
            flow.append(paragraph(label + ": " + value))
    if appointments:
        flow.extend(
            [
                Spacer(1, 4 * mm),
                paragraph(
                    f"Agenda - {len(appointments)} registros", styles["Heading2"]
                ),
                paragraph(
                    "Situação aplicada na migração: concluído, conforme orientação do cliente. O arquivo de origem não informa presença, falta ou cancelamento. Não há informação de pagamento neste histórico."
                ),
            ]
        )
        cells = [
            [paragraph(x) for x in ("Data", "Início / fim", "Min.", "Procedimento")]
        ]
        for row in sorted(appointments, key=lambda r: (r["inicio"], r["id_agenda"])):
            start = local_datetime(row["inicio"], "agenda.inicio", zone)
            end = local_datetime(row["fim"], "agenda.fim", zone)
            cells.append(
                [
                    paragraph(start.strftime("%d/%m/%Y")),
                    paragraph(start.strftime("%H:%M") + " / " + end.strftime("%H:%M")),
                    paragraph(int((end - start).total_seconds() / 60)),
                    paragraph(row["procedimento"]),
                ]
            )
        table = LongTable(
            cells,
            colWidths=[27 * mm, 30 * mm, 14 * mm, 103 * mm],
            repeatRows=1,
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5F3F1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#D6E0E5")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        flow.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#425563"))
        canvas.drawString(
            18 * mm,
            12 * mm,
            "KorusFono - Histórico importado / acesso restrito ao prontuário",
        )
        canvas.drawRightString(192 * mm, 12 * mm, str(doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(
        stream,
        pagesize=(210 * mm, 297 * mm),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=22 * mm,
        title=title,
        author="KorusFono",
        invariant=1,
    )
    doc.build(
        flow,
        onFirstPage=footer,
        onLaterPages=footer,
        canvasmaker=Canvas,
    )
    return stream.getvalue()
