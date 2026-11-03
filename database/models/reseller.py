"""Reseller pricing: what a mobile shop pays us, as opposed to retail.

Two tables because a reseller buys in two different shapes, and one rule cannot
express both:

- a **ready plan** has a fixed price, so the reseller's price is also a fixed
  number — one row per plan × duration × tier they are allowed a special price on;
- **custom volume** is an open calculator, so it needs rates rather than a
  price list. The owner's decision was a raw rate (toman per GB, toman per day)
  rather than a percentage off retail, so that changing a retail price does not
  silently move reseller margin.

`ResellerPlanPrice` keys on the package **slug**, not its row id. Publishing a
catalog edit clones the version and mints new ids, so an id here would come
unstuck from its plan the first time the owner changed a price. The slug
survives the clone; that is the whole reason it exists.

`ResellerPriceOp` holds one undo snapshot for a percent bulk cut on one tab.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class ResellerPlanPrice(Base):
    """What one reseller pays for one ready-plan cell (slug × duration × tier)."""

    __tablename__ = "reseller_plan_prices"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "package_slug",
            "duration_days",
            "tier",
            name="uq_reseller_plan_price_cell",
        ),
        Index("ix_reseller_plan_prices_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="economy")
    price_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ResellerRate(Base):
    """What one reseller pays per GB and per day on a custom volume service."""

    __tablename__ = "reseller_rates"
    __table_args__ = (UniqueConstraint("user_id", name="uq_reseller_rate_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    price_per_gb_toman: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_per_day_toman: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Free trials this reseller may hand out each month before the wallet is
    # charged; `trial_price_toman` is what each extra one costs them.
    trial_quota_monthly: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trial_price_toman: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ResellerPriceOp(Base):
    """One-step undo snapshot for a percent bulk cut on one (duration, tier) tab."""

    __tablename__ = "reseller_price_ops"
    __table_args__ = (
        Index(
            "ix_reseller_price_ops_user_tab",
            "user_id",
            "duration_days",
            "tier",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
