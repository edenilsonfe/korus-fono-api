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
from app.models.family_portal import FamilyPortal, FamilyPortalGrant
from app.models.family_portal_content import (
    FamilyPortalItem,
    FamilyPortalItemAudience,
    FamilyPortalItemRevision,
)
from app.models.professional import Professional
from app.models.report_delivery import ReportDelivery
from app.models.resource import Resource

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Consecutivas do ciclo (head → base do ciclo), terminando na revisão anterior
# ao ciclo (876ae1016496), que fecha a checagem de encadeamento.
CYCLE_CHAIN = [
    "1f2db0ef179a",  # f14_family_portal_publications
    "5116e40d2738",  # f14_family_portal_access
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
    assert heads == {"1f2db0ef179a"}, f"head único esperado; encontrados: {heads}"

    walk: list[str] = []
    current: str | None = "1f2db0ef179a"
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

    # F14: época própria do portal da conta legada = 0 (nenhuma invalidação
    # anterior; 0 NÃO autoriza acesso) e o portal nasce desabilitado.
    epoch = Professional.__table__.c.family_portal_access_version
    assert epoch.nullable is False
    assert epoch.default is not None and epoch.default.arg == 0
    assert str(epoch.server_default.arg) == "0"

    enabled = FamilyPortal.__table__.c.enabled
    assert enabled.nullable is False
    assert enabled.default is not None and enabled.default.arg is False

    grant_columns = FamilyPortalGrant.__table__.c
    assert grant_columns.token_hash.nullable is False
    assert grant_columns.token_hash.default is None
    assert grant_columns.token_hash.server_default is None
    assert grant_columns.revoked_at.nullable is True

    # F14/M2: conteúdo editorial nasce FECHADO — rascunho vazio, sem ponteiro
    # de publicação, sem audiência; revisão é append-only com versão positiva.
    item_status = FamilyPortalItem.__table__.c.status
    assert item_status.nullable is False
    assert item_status.default.arg == "draft"
    assert str(item_status.server_default.arg) == "draft"
    assert str(FamilyPortalItem.__table__.c.version.server_default.arg) == "1"
    assert FamilyPortalItem.__table__.c.published_version.nullable is True
    assert FamilyPortalItem.__table__.c.published_at.nullable is True
    assert FamilyPortalItem.__table__.c.withdrawn_at.nullable is True

    revision = FamilyPortalItemRevision.__table__.c
    assert revision.version.nullable is False
    assert revision.source_fingerprint.nullable is True
    assert revision.expires_at.nullable is True

    audience_pk = FamilyPortalItemAudience.__table__.primary_key
    assert {column.name for column in audience_pk.columns} == {
        "item_id",
        "recipient_id",
    }


# --------------------------------------------------------------------------- #
# Ensaio REAL de schema no PostgreSQL descartável (plano §5.2 / §6.2 4.1)
# --------------------------------------------------------------------------- #

F14_TABLES_DROP_ORDER = [
    "family_portal_item_audiences",
    "family_portal_item_revisions",
    "family_portal_items",
    "family_portal_grants",
    "family_portal_events",
    "family_portal_recipients",
    "family_portals",
]


async def test_f14_migrations_run_for_real_in_disposable_schema(audit_pg_factory):
    """Sentinela pré-F14 + upgrade/downgrade REAIS das revisões M1/M2.

    Roda no schema descartável do gate PostgreSQL (skip sem TEST_AUDIT_PG_URL —
    pulado nunca conta como aprovado). Usa as revisões REALMENTE carregadas do
    disco (nada de DDL reproduzido à mão) e confere que nenhum objeto fora do
    schema descartável foi tocado.
    """
    import importlib.util

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    factory = audit_pg_factory
    engine = factory.kw["bind"]
    async with engine.connect() as conn:
        schema = await conn.scalar(sa.text("select current_schema()"))
    assert isinstance(schema, str) and schema.startswith("audit_test_"), schema

    def _load(prefix: str):
        path = _cycle_file(prefix)
        spec = importlib.util.spec_from_file_location(f"f14_trial_{prefix}", path)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module

    m1 = _load("5116e40d2738")
    m2 = _load("1f2db0ef179a")
    assert m1.revision == "5116e40d2738" and m2.revision == "1f2db0ef179a"
    assert m2.down_revision == m1.revision

    def run_trial(sync_conn) -> None:
        context = MigrationContext.configure(sync_conn)

        def migrate(module, direction: str) -> None:
            with Operations.context(context):
                getattr(module, direction)()

        def table_names() -> set[str]:
            return set(sa.inspect(sync_conn).get_table_names(schema=schema))

        def column_names(table: str) -> set[str]:
            return {
                column["name"]
                for column in sa.inspect(sync_conn).get_columns(
                    table, schema=schema
                )
            }

        # Pré-F14 sintético: remove SÓ os objetos F14 criados pelo create_all e
        # grava um sentinela legado que nenhuma migration pode tocar.
        for table in F14_TABLES_DROP_ORDER:
            sync_conn.execute(
                sa.text(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')
            )
        sync_conn.execute(
            sa.text(
                f'ALTER TABLE "{schema}"."professionals" '
                "DROP COLUMN IF EXISTS family_portal_access_version"
            )
        )
        sync_conn.execute(
            sa.text(
                f'CREATE TABLE "{schema}"."f14_trial_sentinel" '
                "(id integer PRIMARY KEY, note text NOT NULL)"
            )
        )
        sync_conn.execute(
            sa.text(
                f'INSERT INTO "{schema}"."f14_trial_sentinel" (id, note) '
                "VALUES (1, 'legado pre-F14')"
            )
        )
        assert "family_portals" not in table_names()
        assert "family_portal_access_version" not in column_names("professionals")

        # M1 real e M2 real, na ordem da cadeia.
        migrate(m1, "upgrade")
        tables = table_names()
        assert {
            "family_portals",
            "family_portal_recipients",
            "family_portal_events",
            "family_portal_grants",
        } <= tables
        assert "family_portal_access_version" in column_names("professionals")
        assert "family_portal_items" not in tables

        migrate(m2, "upgrade")
        tables = table_names()
        assert {
            "family_portal_items",
            "family_portal_item_revisions",
            "family_portal_item_audiences",
        } <= tables

        # Constraints materiais: default do rascunho, UNIQUE (item, versão),
        # FK real e o índice parcial único "um grant vivo por destinatário".
        item_columns = {
            column["name"]: column
            for column in sa.inspect(sync_conn).get_columns(
                "family_portal_items", schema=schema
            )
        }
        assert "draft" in str(item_columns["status"]["default"])
        uniques = sa.inspect(sync_conn).get_unique_constraints(
            "family_portal_item_revisions", schema=schema
        )
        assert any(
            set(unique["column_names"]) == {"item_id", "version"}
            for unique in uniques
        )
        fks = sa.inspect(sync_conn).get_foreign_keys(
            "family_portal_item_revisions", schema=schema
        )
        assert any(fk["referred_table"] == "family_portal_items" for fk in fks)
        partial = sync_conn.execute(
            sa.text(
                "select indexdef from pg_indexes where schemaname = :schema "
                "and indexname = 'uq_family_portal_grants_active'"
            ),
            {"schema": schema},
        ).scalar()
        assert partial is not None
        assert "WHERE (revoked_at IS NULL)" in " ".join(str(partial).split())

        # Sentinela + legado intactos; nada fora do schema descartável.
        sentinel = sync_conn.execute(
            sa.text(
                f'SELECT note FROM "{schema}"."f14_trial_sentinel" WHERE id = 1'
            )
        ).scalar()
        assert sentinel == "legado pre-F14"
        assert "patients" in table_names()
        leaked = sync_conn.execute(
            sa.text(
                "select count(*) from information_schema.tables "
                "where table_schema = 'public' and table_name like 'family_portal%'"
            )
        ).scalar()
        assert leaked == 0

        # Downgrade real (M2 → M1) e reaplicação completa.
        migrate(m2, "downgrade")
        tables = table_names()
        assert "family_portal_items" not in tables
        assert "family_portals" in tables
        migrate(m1, "downgrade")
        tables = table_names()
        assert "family_portals" not in tables
        assert "family_portal_access_version" not in column_names("professionals")
        assert (
            sync_conn.execute(
                sa.text(
                    f'SELECT note FROM "{schema}"."f14_trial_sentinel" WHERE id = 1'
                )
            ).scalar()
            == "legado pre-F14"
        )

        migrate(m1, "upgrade")
        migrate(m2, "upgrade")
        assert {
            "family_portals",
            "family_portal_items",
            "family_portal_item_audiences",
        } <= table_names()
        assert (
            sync_conn.execute(
                sa.text(
                    f'SELECT note FROM "{schema}"."f14_trial_sentinel" WHERE id = 1'
                )
            ).scalar()
            == "legado pre-F14"
        )

    async with engine.begin() as conn:
        await conn.run_sync(run_trial)
