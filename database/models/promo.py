"""A promo code used by an order: ``promo_redemptions``.

Written in the order's own transaction (``services/promo_redemptions.py``), keyed on the
**code string** (upper-case), never on a catalog row id, so publishing a new catalog version
keeps the count. A code counts only the rows written at or after its ``counted_from`` stamp
(the moment it was created, kept in the pricing document entry), so deleting a code and
creating it again starts from zero. A redemption counts while its order is live
(``DEAD_ORDER_STATUSES`` frees it); nothing rewrites a row when an order dies.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (
        Index("ix_promo_redemptions_code", "code"),
        Index("uq_promo_redemptions_order_id", "order_id", unique=True),
        Index("ix_promo_redemptions_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
