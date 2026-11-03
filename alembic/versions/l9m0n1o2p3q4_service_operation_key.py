"""Caller identity for a manual service operation.

Revision ID: l9m0n1o2p3q4
Revises: k8l9m0n1o2p3
Create Date: 2026-09-23

A pending row already stops a lost reply from recomputing the target. After
the row is applied, the same caller must still be recognizable, or the next
tap is a second command.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l9m0n1o2p3q4"
down_revision: str | None = "k8l9m0n1o2p3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "service_operations",
        sa.Column("operation_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_service_operations_operation_key",
        "service_operations",
        ["operation_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_service_operations_operation_key", table_name="service_operations")
    op.drop_column("service_operations", "operation_key")
