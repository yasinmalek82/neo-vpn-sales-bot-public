"""Add pasarguard_settings.product_group_ids JSON map

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-12

Schema decisions:
- product_group_ids JSON/JSONB nullable map keyed by plan type string
  (e.g. {"volume":[7],"unlimited":[1]}). Extensible for future keys
  (economy_volume, premium_volume, …).
- NULL / missing key → fall back to default_group_ids at resolve time.
- default_group_ids column retained as global fallback.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pasarguard_settings",
        sa.Column(
            "product_group_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("pasarguard_settings", "product_group_ids")
