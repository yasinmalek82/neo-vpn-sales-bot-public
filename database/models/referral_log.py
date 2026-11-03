from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.order import Order
    from database.models.user import User


class ReferralStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    cancelled = "cancelled"


class ReferralLog(Base):
    __tablename__ = "referral_logs"
    __table_args__ = (
        Index("ix_referral_logs_referrer_id", "referrer_id"),
        Index("ix_referral_logs_referred_id", "referred_id"),
        Index("ix_referral_logs_order_id", "order_id"),
        UniqueConstraint("order_id", name="uq_referral_logs_order_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    referred_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    reward_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # discount, traffic_gb, days
    reward_value: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus, name="referral_status", create_constraint=True),
        default=ReferralStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    referrer: Mapped[User] = relationship(
        "User", foreign_keys=[referrer_id], back_populates="referral_logs_referrer"
    )
    referred: Mapped[User] = relationship(
        "User", foreign_keys=[referred_id], back_populates="referral_logs_referred"
    )
    order: Mapped[Order | None] = relationship("Order", back_populates="referral_logs")
