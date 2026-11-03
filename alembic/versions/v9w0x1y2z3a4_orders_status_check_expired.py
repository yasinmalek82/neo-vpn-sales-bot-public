"""Let the SQLite ``orders.status`` CHECK accept EXPIRED.

Revision ID: v9w0x1y2z3a4
Revises: u8v9w0x1y2z3
Create Date: 2026-09-30

Live symptom: ``expire_stale_orders`` raised
``sqlite3.IntegrityError: CHECK constraint failed: order_status`` every five
minutes, so a stale order stayed PENDING_PAYMENT for good.

Root cause: ``c3d4e5f6a7b8`` meant to add EXPIRED by batch-altering the Enum
type, but on SQLite the rebuilt table kept the old *named* CHECK
(``order_status``) and never received the new one. PostgreSQL got
``ALTER TYPE ... ADD VALUE 'EXPIRED'`` and is fine, so this is a no-op there.
Unit tests build the schema with ``create_all`` from the models and never
saw the migrated table.

The value list is a literal on purpose: a migration must not import app models.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v9w0x1y2z3a4"
down_revision: str | None = "u8v9w0x1y2z3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_STATUSES = (
    "PENDING_PAYMENT",
    "PAYMENT_REVIEW",
    "PAYMENT_APPROVED",
    "PROVISIONING",
    "ACTIVE",
    "REJECTED",
    "PROVISION_FAILED",
    "EXPIRED",
)
_OLD_STATUSES = tuple(s for s in _NEW_STATUSES if s != "EXPIRED")
_CHECK_NAME = "order_status"


def _in_list(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace_check(values: tuple[str, ...]) -> None:
    # Batch mode rebuilds the table, carrying columns, defaults, foreign keys,
    # the other CHECKs and every index (partial ones keep their WHERE).
    present = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("orders")}
    with op.batch_alter_table("orders") as batch:
        if _CHECK_NAME in present:
            batch.drop_constraint(_CHECK_NAME, type_="check")
        batch.create_check_constraint(_CHECK_NAME, sa.text(_in_list(values)))


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    _replace_check(_NEW_STATUSES)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    stuck = bind.execute(sa.text("SELECT COUNT(*) FROM orders WHERE status = 'EXPIRED'")).scalar()
    if stuck:
        raise RuntimeError(
            f"Cannot downgrade {revision}: {stuck} order(s) have status 'EXPIRED', "
            "which the older CHECK constraint would reject. Move them to another "
            "status first, or stay on this revision."
        )
    _replace_check(_OLD_STATUSES)
