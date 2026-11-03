"""Phase 1: PasarGuardAccount + OrderStatus alignment

Revision ID: b1c2d3e4f5a6
Revises: e8b22a747940
Create Date: 2026-09-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "e8b22a747940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TO_NEW = {
    "pending": "PENDING_PAYMENT",
    "paid": "PAYMENT_APPROVED",
    "activated": "ACTIVE",
    "expired": "ACTIVE",  # historical; no EXPIRED product state on orders
    "cancelled": "REJECTED",
    "refunded": "REJECTED",
}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # --- OrderStatus realignment ---
    if dialect == "postgresql":
        op.execute("ALTER TYPE order_status RENAME TO order_status_old")
        new_enum = sa.Enum(
            "PENDING_PAYMENT",
            "PAYMENT_REVIEW",
            "PAYMENT_APPROVED",
            "PROVISIONING",
            "ACTIVE",
            "REJECTED",
            "PROVISION_FAILED",
            name="order_status",
        )
        new_enum.create(bind, checkfirst=False)
        op.execute("ALTER TABLE orders ALTER COLUMN status TYPE VARCHAR(32)")
        for old, new in _OLD_TO_NEW.items():
            op.execute(
                sa.text("UPDATE orders SET status = :new WHERE status = :old").bindparams(
                    old=old, new=new
                )
            )
        op.execute(
            "ALTER TABLE orders ALTER COLUMN status TYPE order_status USING status::order_status"
        )
        op.execute("DROP TYPE order_status_old")
    else:
        # SQLite / others: rewrite string values then recreate constraint via batch
        for old, new in _OLD_TO_NEW.items():
            op.execute(
                sa.text("UPDATE orders SET status = :new WHERE status = :old").bindparams(
                    old=old, new=new
                )
            )
        with op.batch_alter_table("orders") as batch:
            batch.alter_column(
                "status",
                existing_type=sa.Enum(
                    name="order_status",
                    native_enum=False,
                ),
                type_=sa.Enum(
                    "PENDING_PAYMENT",
                    "PAYMENT_REVIEW",
                    "PAYMENT_APPROVED",
                    "PROVISIONING",
                    "ACTIVE",
                    "REJECTED",
                    "PROVISION_FAILED",
                    name="order_status",
                    native_enum=False,
                ),
                existing_nullable=False,
                server_default="PENDING_PAYMENT",
            )

    op.create_table(
        "pasarguard_accounts",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("panel_user_id", sa.Integer(), nullable=False),
        sa.Column("panel_username", sa.String(length=64), nullable=False),
        sa.Column("subscription_url", sa.Text(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pasarguard_accounts_user_id", "pasarguard_accounts", ["user_id"], unique=True
    )
    op.create_index(
        "ix_pasarguard_accounts_panel_username",
        "pasarguard_accounts",
        ["panel_username"],
        unique=True,
    )
    op.create_index(
        "ix_pasarguard_accounts_panel_user_id",
        "pasarguard_accounts",
        ["panel_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_pasarguard_accounts_panel_user_id", table_name="pasarguard_accounts")
    op.drop_index("ix_pasarguard_accounts_panel_username", table_name="pasarguard_accounts")
    op.drop_index("ix_pasarguard_accounts_user_id", table_name="pasarguard_accounts")
    op.drop_table("pasarguard_accounts")

    # Downgrade of enum is intentionally limited; restore previous labels best-effort.
    reverse = {v: k for k, v in _OLD_TO_NEW.items()}
    # Prefer first mapping for shared targets
    reverse["ACTIVE"] = "activated"
    reverse["REJECTED"] = "cancelled"
    reverse["PENDING_PAYMENT"] = "pending"
    reverse["PAYMENT_APPROVED"] = "paid"
    reverse["PAYMENT_REVIEW"] = "pending"
    reverse["PROVISIONING"] = "paid"
    reverse["PROVISION_FAILED"] = "cancelled"

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE orders ALTER COLUMN status TYPE VARCHAR(32)")
        for new, old in reverse.items():
            op.execute(
                sa.text("UPDATE orders SET status = :old WHERE status = :new").bindparams(
                    old=old, new=new
                )
            )
        op.execute("DROP TYPE IF EXISTS order_status")
        old_enum = sa.Enum(
            "pending",
            "paid",
            "activated",
            "expired",
            "cancelled",
            "refunded",
            name="order_status",
        )
        old_enum.create(bind, checkfirst=False)
        op.execute(
            "ALTER TABLE orders ALTER COLUMN status TYPE order_status USING status::order_status"
        )
    else:
        for new, old in reverse.items():
            op.execute(
                sa.text("UPDATE orders SET status = :old WHERE status = :new").bindparams(
                    old=old, new=new
                )
            )
