"""Idempotency key for a service the owner creates for a customer.

Revision ID: s6t7u8v9w0x1
Revises: r5s6t7u8v9w0
Create Date: 2026-09-30

«🎁 ساخت سرویس» mints an 8-character key when the confirm screen is drawn and
puts it in the button. The order carries it in a unique index, so a second tap,
a retry after a lost reply, or two racing taps all land on the same order
instead of creating a second service or taking a second wallet debit. NULL on
every existing order; a unique index allows any number of NULLs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s6t7u8v9w0x1"
down_revision: str | None = "r5s6t7u8v9w0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("admin_request_key", sa.String(length=32), nullable=True))
        batch.create_index("uq_orders_admin_request_key", ["admin_request_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_index("uq_orders_admin_request_key")
        batch.drop_column("admin_request_key")
