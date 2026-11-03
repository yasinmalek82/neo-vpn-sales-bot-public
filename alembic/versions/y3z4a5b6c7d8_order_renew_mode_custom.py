"""Mark a renew from scratch, and remember a custom purchase and its store group.

Revision ID: y3z4a5b6c7d8
Revises: x1y2z3a4b5c6
Create Date: 2026-10-04

Three plain additive columns on ``orders``:

- ``renew_mode``: NULL for every order written before this revision and for an
  ordinary extension; ``'reset'`` for a «renew from scratch» (volume and time
  set anew, the leftover dropped, panel groups never written). It sits beside
  ``renew_resets``, which stays the plan switch: one flag per operation, and
  neither is ever inferred from the other or from a slug mismatch.
- ``store_category_slug``: the store group the order was sold from. Slug, not
  row id (publishing mints new ids), so a later renew finds its group without
  guessing from panel groups. NULL on older orders.
- ``custom_purchase``: the order was a custom (گیگ/ماه) purchase, so it can be
  told from a legacy order that merely has no plan slug. False on older orders.

NOTE for the lead: ``y2z3a4b5c6d7`` on another branch has the same parent;
chain one after the other by editing ``down_revision`` of the second.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y3z4a5b6c7d8"
down_revision: str | None = "x1y2z3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("renew_mode", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("store_category_slug", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("custom_purchase", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("custom_purchase")
        batch.drop_column("store_category_slug")
        batch.drop_column("renew_mode")
