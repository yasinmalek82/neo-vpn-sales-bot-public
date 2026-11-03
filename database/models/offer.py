"""A discount for one user, with no code: ``user_offers``.

Written with a marketing nudge (``services/user_offers.py``), keyed by the
notice's ``event_key`` so a retried send reuses the row. Checkout reads it
through ``services/checkout.py``; it is attached to the order that used it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class UserOffer(Base):
    __tablename__ = "user_offers"
    __table_args__ = (
        CheckConstraint("percent BETWEEN 1 AND 50", name="ck_user_offers_percent"),
        Index("uq_user_offers_event_key", "event_key", unique=True),
        Index("uq_user_offers_order_id", "order_id", unique=True),
        Index("ix_user_offers_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign: Mapped[str] = mapped_column(String(24), nullable=False)
    percent: Mapped[int] = mapped_column(Integer, nullable=False)
    # Only an order with strictly more volume than this may use the offer.
    min_volume_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The order that holds it. An order that dies (``DEAD_ORDER_STATUSES``)
    # frees it again, so nothing rewrites this column.
    order_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The recipient refused the message for good: the offer was never seen.
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
