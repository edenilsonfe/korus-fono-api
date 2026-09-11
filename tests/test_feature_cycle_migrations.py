"""Fechamento do ciclo F3/F20/F6/F16/F17 — integridade LOCAL das migrations.

Verifica, sem banco:
- cadeia de revisões: histórico linear, head único e as 6 revisões do ciclo
  consecutivas desde a base 876ae1016496;
- curadoria anti-drift: os arquivos do ciclo não contêm operações das tabelas
  de drift pré-existente (affiliates, app_notifications, battery_*,
  financial_profiles, google_calendar_connections) — os nomes podem aparecer
  apenas na "Nota de revisão" (docstring), nunca em código;
- backfills conservadores: nenhuma revisão do ciclo executa `op.execute`
  (nenhum dado fictício de licença/snapshot/revisão é fabricado);
- defaults legados: colunas novas em tabelas existentes têm default seguro
  (version=1, recipientKind=standard, publicationStatus=draft), e colunas de
  histórico legado são nuláveis (revisão sem número e snapshot ausente não
  inventam dados).

O ensaio REAL de schema (upgrade/downgrade, concorrência, FKs) roda no gate
PostgreSQL (`.run_audit_gate.sh`) e no banco de dev — este arquivo não o
substitui; ele trava a curadoria e os contratos de backfill no CI local.
"""

import re
from pathlib import Path

from app.models.ai import AIReport, AIReportRevision
from app.models.report_delivery import ReportDelivery
from app.models.resource import Resource

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Consecutivas do ciclo (head → base do ciclo), terminando na revisão anterior
# ao ciclo (876ae1016496), que fecha a checagem de encadeamento.
CYCLE_CHAIN = [
    "0630221e18ba",  # f16_home_programs
    "73b5bac60d26",  # f17_resource_licenses_links_cleanup
    "52d2079a38ae",  # f6_patient_record_exports
    "6bca409fe444",  # f20_school_delivery_receipt
    "4147477648e9",  # f3_report_composition_versions
    "55f75dbb1265",  # entrega/identidade/reavaliação (ciclo anterior imediato)
    "876ae1016496",  # base anterior ao ciclo
]

DRIFT_TOKENS = (
    "affiliate",
    "app_notifications",
    "battery_subform",
    "battery_item_evidences",
    "battery_session_events",
    "financial_profiles",
    "google_calendar",
)


def _parse_revisions() -> dict[str, str | None]:
    parsed: dict[str, str | None] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        revision = re.search(
            r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", source, re.M
        )
        down = re.search(
            r"^down_revision(?::[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)",
            source,
            re.M,
        )
        if revision:
            parsed[revision.group(1)] = down.group(1) if down and down.group(1) else None
    return parsed


def _cycle_file(prefix: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"{prefix}_*.py"))
    assert len(matches) == 1, f"esperava exatamente 1 arquivo para {prefix}: {matches}"
    return matches[0]


def _code_only(source: str) -> str:
    """Remove o docstring do módulo e comentários (a Nota de revisão cita o drift)."""
    first = source.find('"""')
    if first != -1:
        second = source.find('"""', first + 3)
        if second != -1:
            source = source[:first] + source[second + 3 :]
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def test_migration_chain_is_linear_with_single_head_and_cycle_in_order():
    revisions = _parse_revisions()
    assert revisions, "nenhuma revisão parseada"

    referenced = {down for down in revisions.values() if down}
    missing = referenced - set(revisions)
    assert not missing, f"down_revision sem arquivo correspondente: {missing}"

    heads = set(revisions) - referenced
    assert heads == {"0630221e18ba"}, f"head único esperado; encontrados: {heads}"

    walk: list[str] = []
    current: str | None = "0630221e18ba"
    while current and current in revisions and current not in walk:
        walk.append(current)
        current = revisions[current]
    assert len(walk) == len(revisions), "histórico deixou de ser linear / há órfãos"
    assert walk[: len(CYCLE_CHAIN)] == CYCLE_CHAIN, (
        "as revisões do ciclo não são consecutivas na ordem esperada: "
        f"{walk[: len(CYCLE_CHAIN) + 1]}"
    )


def test_cycle_migrations_exclude_known_preexisting_drift():
    for prefix in CYCLE_CHAIN[:6]:
        code = _code_only(_cycle_file(prefix).read_text(encoding="utf-8"))
        for token in DRIFT_TOKENS:
            assert token not in code, (
                f"{prefix}: token de drift '{token}' vazou para o código da migration "
                "(curadoria obrigatória — plano §6.3)"
            )


def test_cycle_migrations_do_not_fabricate_data_backfills():
    for prefix in CYCLE_CHAIN[:6]:
        code = _code_only(_cycle_file(prefix).read_text(encoding="utf-8"))
        assert "op.execute" not in code, (
            f"{prefix}: backfill de dados detectado — o ciclo é só schema; "
            "aprovação/snapshot/revisão não podem ser fabricados por migration"
        )


def test_legacy_backfill_defaults_are_conservative():
    # version: linhas legadas passam a valer 1 (revisão inicial de fato existente).
    version = AIReport.__table__.c.version
    assert version.nullable is False
    assert version.default is not None and version.default.arg == 1
    assert str(version.server_default.arg) == "1"

    # revisões históricas sem número permanecem nulas (sem número inventado).
    rev_version = AIReportRevision.__table__.c.version
    assert rev_version.nullable is True
    assert rev_version.default is None and rev_version.server_default is None

    # recipientKind legado = standard (canal F1), nunca school.
    recipient = ReportDelivery.__table__.c.recipient_kind
    assert recipient.nullable is False
    assert recipient.default is not None and recipient.default.arg == "standard"
    assert str(recipient.server_default.arg) == "standard"

    # snapshot textual de entrega: legado nulo (link antigo segue "legacy_live").
    snapshot = ReportDelivery.__table__.c.document_snapshot
    assert snapshot.nullable is True

    # publicação editorial: legado = draft (sem liberação fictícia de catálogo).
    publication = Resource.__table__.c.publication_status
    assert publication.nullable is False
    assert publication.default is not None and publication.default.arg == "draft"
    assert str(publication.server_default.arg) == "draft"

    # demais colunas do ciclo em resources são opcionais (serão preenchidas
    # na transição para published, nunca pela migration).
    assert Resource.__table__.c.content_sha256.nullable is True
    assert Resource.__table__.c.archived_at.nullable is True
