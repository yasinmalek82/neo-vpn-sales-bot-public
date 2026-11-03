"""Add pasarguard_settings.api_key for preferred API key auth

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-09-12

Schema decisions:
- api_key Text nullable; NULL means fall back to env PASARGUARD_API_KEY.
- Preferred over username/password when set (panel X-Api-Key / pg_key_…).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pasarguard_settings", sa.Column("api_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("pasarguard_settings", "api_key")
