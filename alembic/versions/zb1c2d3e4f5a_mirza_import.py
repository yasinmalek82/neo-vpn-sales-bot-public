"""Immutable Mirza source batches and per-record history/mappings.

Revision ID: zb1c2d3e4f5a
Revises: za0b1c2d3e4f
"""

import sqlalchemy as sa
from alembic import op

revision = "zb1c2d3e4f5a"
down_revision = "za0b1c2d3e4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("legacy_trial_consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "mirza_import_batches",
        sa.Column("namespace", sa.String(32), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mirza_import_records",
        sa.Column(
            "namespace",
            sa.String(32),
            sa.ForeignKey("mirza_import_batches.namespace", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("source_table", sa.String(32), primary_key=True),
        sa.Column("source_key", sa.String(128), primary_key=True),
        sa.Column("source_row_sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column(
            "target_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "target_account_id",
            sa.BigInteger(),
            sa.ForeignKey("pasarguard_accounts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "opening_entry_id",
            sa.BigInteger(),
            sa.ForeignKey("wallet_ledger_entries.id", ondelete="RESTRICT"),
        ),
    )
    op.create_index("ix_mirza_import_records_user", "mirza_import_records", ["target_user_id"])


def downgrade() -> None:
    op.drop_table("mirza_import_records")
    op.drop_table("mirza_import_batches")
    op.drop_column("users", "legacy_trial_consumed")
