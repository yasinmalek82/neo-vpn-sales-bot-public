"""Customer disruption reports: ISP, city, services.

Revision ID: q4r5s6t7u8v9
Revises: p3q4r5s6t7u8
Create Date: 2026-09-26

Direct inbounds work on some ISPs and not others. The owner fixes the ISPs
with the most reports first, so reports are rows the nightly digest counts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q4r5s6t7u8v9"
down_revision: str | None = "p3q4r5s6t7u8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "disruption_reports",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("isp", sa.String(length=32), nullable=False),
        sa.Column("city", sa.String(length=64), nullable=False),
        sa.Column("service_groups", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_disruption_reports_user_id", "disruption_reports", ["user_id"])
    op.create_index("ix_disruption_reports_created_at", "disruption_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_disruption_reports_created_at", table_name="disruption_reports")
    op.drop_index("ix_disruption_reports_user_id", table_name="disruption_reports")
    op.drop_table("disruption_reports")
