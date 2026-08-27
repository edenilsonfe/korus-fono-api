"""o6p7q8r9s0t1 — Admin RBAC and durable actor snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "o6p7q8r9s0t1"
down_revision = "n5o6p7q8r9s0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("professionals", sa.Column("admin_role", sa.String(length=32), nullable=True))
    op.create_index("ix_professionals_admin_role", "professionals", ["admin_role"], unique=False)
    op.create_check_constraint(
        "ck_professionals_admin_role",
        "professionals",
        "admin_role IS NULL OR admin_role IN ('support', 'billing', 'product', 'superadmin')",
    )
    op.execute("UPDATE professionals SET admin_role = 'superadmin' WHERE is_staff IS TRUE")

    op.add_column("admin_audit_logs", sa.Column("actor_name", sa.String(length=255), nullable=True))
    op.add_column("admin_audit_logs", sa.Column("actor_email", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE admin_audit_logs AS log
        SET actor_name = professional.name,
            actor_email = professional.email
        FROM professionals AS professional
        WHERE professional.id = log.actor_id
        """
    )
    op.drop_constraint(
        "admin_audit_logs_actor_id_fkey",
        "admin_audit_logs",
        type_="foreignkey",
    )
    op.alter_column(
        "admin_audit_logs",
        "actor_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_foreign_key(
        "admin_audit_logs_actor_id_fkey",
        "admin_audit_logs",
        "professionals",
        ["actor_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "admin_audit_logs_actor_id_fkey",
        "admin_audit_logs",
        type_="foreignkey",
    )
    op.execute("DELETE FROM admin_audit_logs WHERE actor_id IS NULL")
    op.alter_column(
        "admin_audit_logs",
        "actor_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "admin_audit_logs_actor_id_fkey",
        "admin_audit_logs",
        "professionals",
        ["actor_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("admin_audit_logs", "actor_email")
    op.drop_column("admin_audit_logs", "actor_name")
    op.drop_constraint("ck_professionals_admin_role", "professionals", type_="check")
    op.drop_index("ix_professionals_admin_role", table_name="professionals")
    op.drop_column("professionals", "admin_role")
