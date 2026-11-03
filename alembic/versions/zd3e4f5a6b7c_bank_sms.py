"""Permanent exact-amount invoices and deduplicated bank notification audit.

Revision ID: zd3e4f5a6b7c
Revises: zc2d3e4f5a6b
"""

import sqlalchemy as sa
from alembic import op

revision = "zd3e4f5a6b7c"
down_revision = "zc2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_invoices",
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.Column("card_digest", sa.String(64), nullable=False),
        sa.Column("base_due_toman", sa.BigInteger(), nullable=False),
        sa.Column("suffix_toman", sa.BigInteger(), nullable=False),
        sa.Column("amount_toman", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_key", "amount_toman", name="uq_bank_invoice_account_amount"),
        sa.CheckConstraint("suffix_toman BETWEEN 1 AND 999", name="ck_bank_invoice_suffix"),
        sa.CheckConstraint(
            "base_due_toman > 0 AND amount_toman = base_due_toman + suffix_toman",
            name="ck_bank_invoice_amount",
        ),
    )
    op.create_table(
        "bank_sms_events",
        sa.Column("event_key", sa.String(64), primary_key=True),
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.Column("amount_toman", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bank_sms_events")
    op.drop_table("bank_invoices")
