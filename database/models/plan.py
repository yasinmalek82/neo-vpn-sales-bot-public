from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base

if TYPE_CHECKING:
    from database.models.order import Order


class DataLimitResetStrategy(str, enum.Enum):
    no_reset = "no_reset"
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_is_active_sort", "is_active", "sort_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_limit_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_toman: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_limit_reset_strategy: Mapped[DataLimitResetStrategy] = mapped_column(
        Enum(DataLimitResetStrategy, name="data_limit_reset_strategy", create_constraint=True),
        default=DataLimitResetStrategy.no_reset,
        nullable=False,
    )
    # Panel group IDs for this plan (JSON array of integers)
    group_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )
    # HWID/device limit for this plan (number of simultaneous connections)
    hwid_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    orders: Mapped[list[Order]] = relationship("Order", back_populates="plan")
