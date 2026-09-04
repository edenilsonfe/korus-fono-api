"""Validate the isolated additive migration without connecting to a shared database."""

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def load_migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/y6z7a8b9c0d1_professional_temporary_access.py"
    )
    spec = importlib.util.spec_from_file_location("temporary_access_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_downgrade_preserves_existing_accounts():
    migration = load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE professionals (id INTEGER PRIMARY KEY, name VARCHAR(255))"
            )
        )
        connection.execute(
            text("INSERT INTO professionals (id, name) VALUES (1, 'Existing account')")
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.execute(
                    text("SELECT temporary_access_ends_at FROM professionals")
                ).scalar_one()
                is None
            )
            migration.downgrade()
        assert [
            c["name"] for c in inspect(connection).get_columns("professionals")
        ] == ["id", "name"]
        assert (
            connection.execute(
                text("SELECT name FROM professionals WHERE id=1")
            ).scalar_one()
            == "Existing account"
        )
    engine.dispose()


def test_postgresql_ddl_only_adds_nullable_expiry():
    migration = load_migration()
    output = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    ):
        migration.upgrade()
    sql = output.getvalue().strip()
    assert (
        sql
        == "ALTER TABLE professionals ADD COLUMN temporary_access_ends_at TIMESTAMP WITH TIME ZONE;"
    )
