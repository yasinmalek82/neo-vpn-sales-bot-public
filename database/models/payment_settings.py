"""Singleton row for the card the bot shows customers (P4).

NULL in a column means «use the .env value», exactly like ``pasarguard_settings``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

PAYMENT_SETTINGS_ROW_ID = 1


class PaymentSettings(Base):
    """DB override for the card-to-card payment display; env is the fallback."""

    __tablename__ = "payment_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    card_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    card_holder_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        nullable=True,
    )
