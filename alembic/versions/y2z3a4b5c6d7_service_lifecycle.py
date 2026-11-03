"""Warnings before a service ends, and deleting it after.

Revision ID: y2z3a4b5c6d7
Revises: y3z4a5b6c7d8
Create Date: 2026-10-03

* ``lifecycle_settings``: one row (id = 1) with the owner's numbers. An empty
  table means the defaults, so a running shop changes nothing until the owner
  opens the screen. ``delete_mode`` starts ``off``.
* ``service_notices``: the claim marker of every lifecycle message to a
  customer (unique ``event_key``).
* ``pasarguard_accounts.limited_since``, ``deleted_at`` and
  ``delete_requested_at`` (nullable, empty for every existing row, so every
  existing service stays live) and ``delete_exempt`` (false for every row).
* ``lifecycle_batches`` / ``lifecycle_batch_items``: the list the owner approves
  by hand when a run stops at the hard stop.

Plain DDL only (no dialect-specific SQL), so SQLite and PostgreSQL both run it.
The downgrade refuses while a service is soft-deleted: dropping ``deleted_at``
would bring back a service whose panel user is gone.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# The same type the model binds (`JSON().with_variant(JSONB, "postgresql")`) and the same
# spelling the project's other JSON columns use in their migrations (initial migration,
# pasarguard_settings): JSON on SQLite, JSONB on PostgreSQL.
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

revision: str = "y2z3a4b5c6d7"
down_revision: str | None = "y3z4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("warn_days", _JSON, nullable=False, server_default=sa.text("'[3, 1]'")),
        sa.Column(
            "warn_volume_percents", _JSON, nullable=False, server_default=sa.text("'[20, 5]'")
        ),
        sa.Column("delete_mode", sa.String(length=16), nullable=False, server_default="off"),
        sa.Column("delete_after_expiry_days", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("delete_after_limited_days", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("delete_trial_after_days", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("delete_warn_days_before", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_rehearsal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("delete_mode IN ('off', 'rehearsal', 'on')", name="ck_lifecycle_mode"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "service_notices",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column(
            "account_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undelivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["account_id"], ["pasarguard_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_service_notices_event_key", "service_notices", ["event_key"], unique=True)
    op.create_index("ix_service_notices_account_id", "service_notices", ["account_id"])
    op.create_index(
        "ix_service_notices_unsent",
        "service_notices",
        ["kind"],
        sqlite_where=sa.text("sent_at IS NULL"),
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.add_column(
        "pasarguard_accounts", sa.Column("limited_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "pasarguard_accounts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "pasarguard_accounts",
        sa.Column("delete_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pasarguard_accounts",
        sa.Column("delete_exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "lifecycle_batches",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("numbers_key", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('stop', 'manual', 'preview')", name="ck_lifecycle_batch_kind"),
        sa.CheckConstraint("status IN ('open', 'used', 'void')", name="ck_lifecycle_batch_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "lifecycle_batch_items",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("batch_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "account_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("reason_code", sa.String(length=16), nullable=False),
        sa.Column("reason_days", sa.Integer(), nullable=True),
        sa.Column("after_warning", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("action IN ('delete', 'warn')", name="ck_lifecycle_item_action"),
        sa.ForeignKeyConstraint(["batch_id"], ["lifecycle_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["pasarguard_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_lifecycle_batch_items_batch_account",
        "lifecycle_batch_items",
        ["batch_id", "account_id"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    stuck = bind.execute(
        sa.text("SELECT COUNT(*) FROM pasarguard_accounts WHERE deleted_at IS NOT NULL")
    ).scalar()
    if stuck:
        raise RuntimeError(
            f"Cannot downgrade {revision}: {stuck} service(s) are soft-deleted, and dropping "
            "deleted_at would bring back services whose panel user is gone."
        )
    op.drop_index("uq_lifecycle_batch_items_batch_account", table_name="lifecycle_batch_items")
    op.drop_table("lifecycle_batch_items")
    op.drop_table("lifecycle_batches")
    with op.batch_alter_table("pasarguard_accounts") as batch:
        batch.drop_column("delete_exempt")
        batch.drop_column("delete_requested_at")
        batch.drop_column("deleted_at")
        batch.drop_column("limited_since")
    op.drop_index("ix_service_notices_unsent", table_name="service_notices")
    op.drop_index("ix_service_notices_account_id", table_name="service_notices")
    op.drop_index("uq_service_notices_event_key", table_name="service_notices")
    op.drop_table("service_notices")
    op.drop_table("lifecycle_settings")
