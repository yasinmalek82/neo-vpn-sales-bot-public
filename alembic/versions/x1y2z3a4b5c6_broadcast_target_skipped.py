"""Let ``broadcast_targets.state`` accept ``skipped``.

Revision ID: x1y2z3a4b5c6
Revises: w0x1y2z3a4b5
Create Date: 2026-09-30

A recipient banned after the campaign started is marked ``skipped`` instead of
being sent to. SQLite carries the value list in a named CHECK
(``broadcast_delivery_state``), rebuilt here; PostgreSQL gets
``ALTER TYPE ... ADD VALUE``. The value lists are literals on purpose: a
migration must not import app models.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x1y2z3a4b5c6"
down_revision: str | None = "w0x1y2z3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = ("pending", "sent", "blocked", "failed", "skipped")
_OLD = ("pending", "sent", "blocked", "failed")
_CHECK_NAME = "broadcast_delivery_state"


def _replace_check(values: tuple[str, ...]) -> None:
    in_list = "state IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    present = {
        c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("broadcast_targets")
    }
    with op.batch_alter_table("broadcast_targets") as batch:
        if _CHECK_NAME in present:
            batch.drop_constraint(_CHECK_NAME, type_="check")
        batch.create_check_constraint(_CHECK_NAME, sa.text(in_list))


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _replace_check(_NEW)
    elif dialect == "postgresql":
        op.execute("ALTER TYPE broadcast_delivery_state ADD VALUE IF NOT EXISTS 'skipped'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return  # PostgreSQL cannot drop an enum value; the extra value is harmless.
    stuck = bind.execute(
        sa.text("SELECT COUNT(*) FROM broadcast_targets WHERE state = 'skipped'")
    ).scalar()
    if stuck:
        raise RuntimeError(
            f"Cannot downgrade {revision}: {stuck} recipient(s) are 'skipped', which the "
            "older CHECK constraint would reject."
        )
    _replace_check(_OLD)
