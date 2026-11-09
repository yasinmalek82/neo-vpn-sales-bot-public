"""Persist the operator's automatic bank confirmation switch."""

import sqlalchemy as sa
from alembic import op

revision = "ze4f5a6b7c8d"
down_revision = "zd3e4f5a6b7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_settings", sa.Column("bank_sms_auto", sa.Boolean(), nullable=True))
    # Keep a lockable singleton even on shops that have never edited the card.
    op.execute(
        "INSERT INTO payment_settings (id, updated_at) "
        "SELECT 1, CURRENT_TIMESTAMP WHERE NOT EXISTS (SELECT 1 FROM payment_settings WHERE id = 1)"
    )


def downgrade() -> None:
    op.drop_column("payment_settings", "bank_sms_auto")
