from pathlib import Path


def test_admin_operations_migration_extends_current_head() -> None:
    migration = Path("alembic/versions/o6p7q8r9s0t1_admin_operations_rbac.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "o6p7q8r9s0t1"' in migration
    assert 'down_revision = "n5o6p7q8r9s0"' in migration
    assert '"admin_role"' in migration
    assert '"actor_name"' in migration
    assert '"actor_email"' in migration
    assert "ck_professionals_admin_role" in migration
