"""One personal trial per store group, and a reset that actually resets.

Revision ID: o2p3q4r5s6t7
Revises: n1o2p3q4r5s6
Create Date: 2026-09-26

The owner sells two products that need testing separately (direct unlimited,
tunnel volume), so a personal trial is now keyed on the store group's slug.
``trial_category_slug`` is NULL on every trial issued before this revision;
those count as the direct product's trial (services/trial.py).

``trial_voided_at`` is what the admin «reset trial» writes. Before it, reset
cleared ``users.trial_claimed_at`` only, the old order still matched, and the
next tap resumed the old trial instead of issuing a new one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o2p3q4r5s6t7"
down_revision: str | None = "n1o2p3q4r5s6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE_PERSONAL = "trial_kind = 'personal' AND trial_voided_at IS NULL"


def upgrade() -> None:
    op.add_column("orders", sa.Column("trial_category_slug", sa.String(length=64), nullable=True))
    op.add_column("orders", sa.Column("trial_voided_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_index("uq_orders_personal_trial_user", table_name="orders")
    op.create_index(
        "uq_orders_personal_trial_user_group",
        "orders",
        ["user_id", "trial_category_slug"],
        unique=True,
        sqlite_where=sa.text(_LIVE_PERSONAL),
        postgresql_where=sa.text(_LIVE_PERSONAL),
    )


def downgrade() -> None:
    # The old index allows one personal trial per user. Rebuilding it over two
    # would fail half-way through; say why instead.
    bind = op.get_bind()
    doubled = bind.execute(sa.text("""
            SELECT user_id FROM orders
            WHERE trial_kind = 'personal'
            GROUP BY user_id HAVING COUNT(*) > 1
            LIMIT 1
            """)).first()
    if doubled is not None:
        raise RuntimeError(
            "uq_orders_personal_trial_user refused: a user holds more than one "
            "personal trial; settle those rows by hand before downgrading"
        )
    op.drop_index("uq_orders_personal_trial_user_group", table_name="orders")
    op.create_index(
        "uq_orders_personal_trial_user",
        "orders",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("trial_kind = 'personal'"),
        postgresql_where=sa.text("trial_kind = 'personal'"),
    )
    op.drop_column("orders", "trial_voided_at")
    op.drop_column("orders", "trial_category_slug")
