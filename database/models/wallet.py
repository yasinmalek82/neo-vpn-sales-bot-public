"""Customer wallet balance + immutable ledger entries."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.order import Order
    from database.models.user import User


class WalletEntryType(str, enum.Enum):
    credit = "credit"
    debit = "debit"


class WalletLedgerEntry(Base):
    __tablename__ = "wallet_ledger_entries"
    __table_args__ = (
        Index("ix_wallet_ledger_user_id", "user_id"),
        Index("ix_wallet_ledger_order_id", "order_id"),
        Index("ix_wallet_ledger_idempotency_key", "idempotency_key", unique=True),
        Index("ix_wallet_ledger_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    entry_type: Mapped[WalletEntryType] = mapped_column(
        Enum(WalletEntryType, name="wallet_entry_type", create_constraint=True),
        nullable=False,
    )
    amount_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_after_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="wallet_ledger_entries")
    order: Mapped[Order | None] = relationship("Order")
