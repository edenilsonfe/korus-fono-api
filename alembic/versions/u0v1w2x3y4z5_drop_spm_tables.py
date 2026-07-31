"""Drop SPM battery tables (protocol removed from product).

Revision ID: u0v1w2x3y4z5
Revises: t9u0v1w2x3y4
Create Date: 2026-07-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "u0v1w2x3y4z5"
down_revision: Union[str, None] = "t9u0v1w2x3y4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_flag_overrides WHERE flag_key = 'spm'"))
    op.execute(sa.text("DELETE FROM feature_flags WHERE key = 'spm'"))
    op.drop_index("ix_spm_informant_links_subform_active", table_name="spm_informant_links")
    op.drop_table("spm_informant_links")
    op.drop_index("ix_spm_subform_battery_slug", table_name="spm_subform_assessments")
    op.drop_table("spm_subform_assessments")


def downgrade() -> None:
    # Tables are not recreated here — restore from e5f6a7b8c9d0 if needed.
    raise NotImplementedError("SPM tables were removed; restore from e5f6a7b8c9d0 if required")
