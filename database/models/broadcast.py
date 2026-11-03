"""Broadcasts, with one row per recipient.

The per-recipient row is what makes a broadcast survive a restart. Telegram
sends are slow (roughly twenty a second before rate limits bite), so a send to
a few thousand customers spans minutes — long enough for a deploy, a crash or
a `Ctrl+C` to land in the middle of it. Without a durable record of who has
already received the message, resuming means either spamming everyone again or
abandoning the rest.

The audience is frozen into those rows at creation time, so a customer who
signs up mid-send does not receive a campaign that was never aimed at them.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class BroadcastAudience(str, enum.Enum):
    """Who a broadcast is aimed at."""

    all_customers = "all_customers"
    with_service = "with_service"
    expiring_soon = "expiring_soon"
    without_service = "without_service"


class BroadcastState(str, enum.Enum):
    draft = "draft"
    sending = "sending"
    done = "done"
    cancelled = "cancelled"


class DeliveryState(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    blocked = "blocked"  # the customer blocked the bot — never worth retrying
    failed = "failed"
    skipped = "skipped"  # banned after the campaign started — never sent


class Broadcast(Base):
    """One campaign."""

    __tablename__ = "broadcasts"
    __table_args__ = (Index("ix_broadcasts_state", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[BroadcastAudience] = mapped_column(
        Enum(BroadcastAudience, name="broadcast_audience", create_constraint=True),
        nullable=False,
    )
    state: Mapped[BroadcastState] = mapped_column(
        Enum(BroadcastState, name="broadcast_state", create_constraint=True),
        nullable=False,
        default=BroadcastState.draft,
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BroadcastTarget(Base):
    """One recipient of one broadcast.

    ``(broadcast_id, user_id)`` is unique, so re-running the worker after a
    crash cannot deliver the same campaign to the same person twice.
    """

    __tablename__ = "broadcast_targets"
    __table_args__ = (
        UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_targets_pair"),
        Index("ix_broadcast_targets_pending", "broadcast_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[DeliveryState] = mapped_column(
        Enum(DeliveryState, name="broadcast_delivery_state", create_constraint=True),
        nullable=False,
        default=DeliveryState.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
