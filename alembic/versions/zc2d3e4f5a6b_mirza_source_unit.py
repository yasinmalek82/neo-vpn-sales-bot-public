"""Retain explicit source currency for independent legacy-history rendering.

Revision ID: zc2d3e4f5a6b
Revises: zb1c2d3e4f5a
"""

import sqlalchemy as sa
from alembic import op

revision = "zc2d3e4f5a6b"
down_revision = "zb1c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing batches have an unknown unit until separately reconciled; never
    # guess toman or derive it from the current wallet balance.
    with op.batch_alter_table("mirza_import_batches") as batch:
        batch.add_column(sa.Column("source_unit", sa.String(8), nullable=True))
        batch.create_check_constraint(
            "ck_mirza_source_unit", "source_unit IS NULL OR source_unit IN ('toman', 'rial')"
        )


def downgrade() -> None:
    with op.batch_alter_table("mirza_import_batches") as batch:
        batch.drop_constraint("ck_mirza_source_unit", type_="check")
        batch.drop_column("source_unit")
