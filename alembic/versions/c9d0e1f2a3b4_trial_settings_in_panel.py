"""Move the free-trial shape into pasarguard_settings

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-19

Why: the trial account is created on the panel, so its size belongs beside the
panel settings that provision it. Until now TRIAL_DATA_LIMIT_GB / _DURATION_DAYS
/ _HWID_LIMIT lived only in .env, so changing the trial meant editing a file and
restarting the bot.

All three columns are nullable and NULL means «fall back to the env value», so
this migration changes no behaviour on its own and an existing deployment keeps
running on its .env until the owner edits the trial from Telegram.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("trial_data_limit_gb", "trial_duration_days", "trial_hwid_limit")


def upgrade() -> None:
    with op.batch_alter_table("pasarguard_settings") as batch:
        for name in _COLUMNS:
            batch.add_column(sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pasarguard_settings") as batch:
        for name in _COLUMNS:
            batch.drop_column(name)
