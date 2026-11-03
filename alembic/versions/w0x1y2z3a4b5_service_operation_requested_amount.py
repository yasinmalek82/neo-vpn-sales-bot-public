"""Remember what a manual service operation asked for.

Revision ID: w0x1y2z3a4b5
Revises: v9w0x1y2z3a4
Create Date: 2026-09-30

A pending extend/add-data row is re-applied on retry. Without the requested
days/GB on the row, a failed 7-day tap silently answered the next 30-day
request with the old target (AUDIT-PROV-3). NULL on rows written before this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w0x1y2z3a4b5"
down_revision: str | None = "v9w0x1y2z3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_operations",
        sa.Column("requested_amount", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("service_operations") as batch:
        batch.drop_column("requested_amount")
